"""One-shot operator repair for legacy foreign-owned spend views.

Explicitly invoked by a database administrator; never run by the proxy or by
any migration job. The production defect this repairs: the seven spend
aggregation views were created by an operator (as ``postgres``) rather than by
the application role, so the application role cannot read them and
``prisma migrate`` cannot change them, because a non-superuser cannot take
ownership of another role's objects.

The script:

1. connects using the CNPG superuser mechanism (the pod's local trust auth or
   an operator-supplied connection target), never by reading a password from
   the application's ``DATABASE_URL``;
2. prechecks the target role exists, is not a superuser-borrowed role, matches
   the role the gateway actually connects as, and that no view has dependent
   objects (rules, triggers, materialized views) that a drop would cascade to;
3. snapshots each view's owner, relkind, ACL and definition to a JSON file on
   the operator's machine (never inside the database, never with credentials);
4. in ONE bounded transaction: CREATE OR REPLACEs the six column-stable views
   and drops/recreates only ``DailyTagSpend`` (the one view whose legacy shape
   lacks columns CREATE OR REPLACE cannot add), reassigns ownership of all
   seven to the application role, restores any non-owner reader grants the
   views carried, and re-runs the exact guarded migration SQL under
   ``SET ROLE <app role>`` proving the application role can now converge the
   views itself. If any step fails the whole transaction rolls back: no
   partially-repaired state survives;
5. after that transaction commits (and only then), verifies the post-state
   catalog as the superuser, then opens a SEPARATE connection as the
   application role for the live read proof, so a verification failure is
   reported as "DDL committed but verification failed", never as total failure;
6. per-view reads are bounded: the six ordinary views get a trivial SELECT,
   and the tag view is read only through a checked narrow date window, never
   as an unbounded scan of retained tag history.

Fails closed on any precondition: unknown role, mismatched gateway role,
non-view object, dependent objects, unexpected schema, or a lock that cannot
be obtained within the bounded wait. Never prints connection strings or
credentials.

Usage (from the repo root, with superuser access to the target database):

    python scripts/repair_legacy_spend_views.py \
        --superuser-url postgresql://postgres@127.0.0.1:15433/litellm \
        --app-role litellm \
        --gateway-url-env DATABASE_URL   # optional cross-check, value only read in memory

The superuser connection is operator-supplied (CNPG pod trust auth or a
port-forward with the superuser secret). The app-role password is never needed:
ownership changes and grants run as the superuser and the final readability
proof runs under a SET ROLE session, which requires only membership, not the
role's password.
"""

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final, LiteralString, NamedTuple
from urllib.parse import urlparse

import psycopg
from psycopg import sql

from litellm.types.llms.base import LiteLLMBaseModel

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from litellm.proxy.db.spend_view_definitions import (  # noqa: E402
    SPEND_VIEW_NAMES,
    spend_view_ddl,
)
from scripts.render_spend_view_migration import (  # noqa: E402
    MIGRATION_PATH,
    render_migration_sql,
)

_SNAPSHOT_COLUMNS: Final = ("owner", "relkind", "relacl", "definition")

_TAG_VIEW: Final = "DailyTagSpend"


def _reader_grants(conn: psycopg.Connection, schema: str, name: str, owner: str) -> list[tuple[str, str]]:
    """Non-owner principals with an explicit grant on one view, as (grantee, privilege).

    The owner's own ACL entry is excluded: owner access is implicit in Postgres and
    the repair end state keeps it that way. Everything else (a read-only dashboard
    role, a replication helper, an operator group) is a grant an earlier operator
    installed on purpose, so the repair recreates it after replacing the view rather
    than silently stripping readers.
    """
    rows: list[tuple[str, str]] = conn.execute(
        """
        SELECT pg_get_userbyid(a.grantee), a.privilege_type
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl, acldefault('r'::"char", c.relowner))) AS a
        WHERE n.nspname = %s AND c.relname = %s
          AND pg_get_userbyid(a.grantee) <> %s
        ORDER BY 1, 2
        """,
        (schema, name, owner),
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def _check_no_dependents(conn: psycopg.Connection, schema: str, name: str) -> None:
    """Refuse to touch a view that other catalog objects depend on internally.

    A materialized view over this one (its rewrite rule references our view) or
    another view selecting from it would make DROP VIEW either cascade or fail;
    the repair must never cascade, so any such dependency is a hard stop. The
    view's own ON SELECT rule is excluded: every view has one, it *is* the view.
    """
    dependents: list[tuple[object, ...]] = conn.execute(
        """
        SELECT DISTINCT rn.nspname || '.' || rc.relname
        FROM pg_depend dep
        JOIN pg_class ref ON ref.oid = dep.refobjid
        JOIN pg_namespace n ON n.oid = ref.relnamespace
        JOIN pg_rewrite rw ON rw.oid = dep.objid
        JOIN pg_class rc ON rc.oid = rw.ev_class
        JOIN pg_namespace rn ON rn.oid = rc.relnamespace
        WHERE n.nspname = %s AND ref.relname = %s
          AND dep.classid = 'pg_rewrite'::regclass
          AND rc.oid <> ref.oid
        """,
        (schema, name),
    ).fetchall()
    if dependents:
        names: str = ", ".join(str(d[0]) for d in dependents)
        raise RuntimeError(
            f"view {schema}.{name} has dependent objects ({names}); the repair never cascades, so resolve them first"
        )


def _view_state(conn: psycopg.Connection, schema: str, name: str) -> tuple[str, str, str | None, str | None]:
    row = conn.execute(
        """
        SELECT pg_get_userbyid(c.relowner), c.relkind, c.relacl::text, pg_get_viewdef(c.oid)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s
        """,
        (schema, name),
    ).fetchone()
    if row is None:
        return ("", "", None, None)
    return (row[0], row[1], row[2], row[3])


def _scalar(conn: psycopg.Connection, query: LiteralString, *params: object) -> object:
    row: tuple[object, ...] | None = conn.execute(query, params).fetchone()
    if row is None:
        raise RuntimeError(f"query returned no row: {query}")
    return row[0]


def _precheck(conn: psycopg.Connection, schema: str, app_role: str, gateway_role: str | None) -> None:
    db_role: str = str(_scalar(conn, "SELECT current_user"))
    if not bool(_scalar(conn, "SELECT rolsuper FROM pg_roles WHERE rolname = %s", db_role)):
        raise RuntimeError("repair must run as a database superuser")
    app_row: tuple[object, ...] | None = conn.execute(
        "SELECT rolname, rolsuper FROM pg_roles WHERE rolname = %s", (app_role,)
    ).fetchone()
    if app_row is None:
        raise RuntimeError(f"application role {app_role!r} does not exist")
    if app_row[1]:
        raise RuntimeError(f"refusing to hand the views to {app_role!r}: that role is a superuser")
    if gateway_role is not None and gateway_role != app_role:
        raise RuntimeError(
            f"application role {app_role!r} does not match the gateway's configured role {gateway_role!r}"
        )
    if not conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone():
        raise RuntimeError(f"schema {schema!r} does not exist")
    for name in SPEND_VIEW_NAMES:
        owner, relkind, _, _ = _view_state(conn, schema, name)
        if owner == "":
            continue
        if relkind != "v":
            raise RuntimeError(f"object {schema}.{name} is relkind {relkind!r}, not a view; refusing to alter it")
        if owner not in (db_role, app_role):
            raise RuntimeError(
                f"view {schema}.{name} is owned by {owner!r}, neither the running superuser {db_role!r} "
                f"nor the target application role {app_role!r}; refusing to touch a third party's object"
            )
        if name == _TAG_VIEW or name == "Last30dTopEndUsersSpend":
            _check_no_dependents(conn, schema, name)


def _snapshot(conn: psycopg.Connection, schema: str, out_path: Path) -> None:
    states = {name: dict(zip(_SNAPSHOT_COLUMNS, _view_state(conn, schema, name))) for name in SPEND_VIEW_NAMES}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(states, indent=2, sort_keys=True))
    print(f"pre-repair snapshot written to {out_path} (no credentials included)")  # noqa: T201


def _repair(
    conn: psycopg.Connection,
    schema: str,
    app_role: str,
) -> None:
    """Converge the seven views inside the caller's transaction.

    Only ``DailyTagSpend`` is dropped: its legacy shape lacks columns that
    ``CREATE OR REPLACE VIEW`` cannot add, so it must be recreated. The other
    six have a stable column list and are replaced in place, which preserves
    their OID-dependent state (comments, policies) and cannot cascade.
    Reader grants are NOT restored here: the guarded migration proof below
    drops and recreates the tag view once more, which would strip them, so
    grant restoration runs after that proof instead.
    """
    for name in SPEND_VIEW_NAMES:
        target = sql.Identifier(schema, name)
        if name == _TAG_VIEW:
            conn.execute(sql.SQL("DROP VIEW IF EXISTS {}").format(target))
        elif name == "Last30dTopEndUsersSpend":
            # Its authoritative DDL is a plain CREATE VIEW (upstream shape), so a
            # pre-existing copy must be dropped first; unlike the tag view this is
            # a same-shape replacement, done only because OR REPLACE is not in its
            # definition. No dependents exist or the precheck already refused.
            conn.execute(sql.SQL("DROP VIEW IF EXISTS {}").format(target))
        conn.execute(sql.SQL("{}").format(sql.SQL(spend_view_ddl(name))))  # pyright: ignore[reportArgumentType]  # psycopg wants LiteralString for raw SQL; the DDL is a repo-owned constant, not user input
        conn.execute(sql.SQL("ALTER VIEW {} OWNER TO {}").format(target, sql.Identifier(app_role)))
        action = "recreated" if name == _TAG_VIEW else "replaced"
        print(f"{action} {schema}.{name}, owner set to {app_role}")  # noqa: T201


def _restore_reader_grants(
    conn: psycopg.Connection,
    schema: str,
    reader_grants: Mapping[str, tuple[tuple[str, str], ...]],
) -> None:
    """Re-issue the recorded reader grants after the guarded migration proof.

    This runs after the migration's own tag drop/recreate (inside the same
    transaction), because that recreate builds a fresh default ACL and would
    strip any grant restored earlier. Owner access stays implicit.
    """
    for name, grants in reader_grants.items():
        target = sql.Identifier(schema, name)
        for grantee, privilege in grants:
            conn.execute(sql.SQL("GRANT {} ON {} TO {}").format(sql.SQL(privilege), target, sql.Identifier(grantee)))  # pyright: ignore[reportArgumentType]  # privilege strings come from aclexplode over the operator's own catalog, restricted to known grant privilege names


def _run_guarded_migration_as_app_role(conn: psycopg.Connection, app_role: str) -> None:
    """Re-run the guarded migration under the app role, inside the repair transaction.

    SET LOCAL scopes the role change to this transaction, so the superuser
    session cannot leak the borrowed role past a rollback. The migration's own
    DO block runs CREATE OR REPLACE and the tag drop/recreate again: that is
    deliberate, it proves the application role can now converge the views
    itself, which is exactly what the next pod's ``prisma migrate deploy`` does.
    """
    rendered: str = render_migration_sql()
    if not MIGRATION_PATH.is_file() or MIGRATION_PATH.read_text() != rendered:
        raise RuntimeError("the shipped migration.sql does not match the authoritative view definitions")
    session_user_row: tuple[object, ...] | None = conn.execute("SELECT session_user").fetchone()
    session_user: str = str(session_user_row[0]) if session_user_row else ""
    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(app_role)))
    current_row: tuple[object, ...] | None = conn.execute("SELECT current_user").fetchone()
    current: str = str(current_row[0]) if current_row else ""
    if current != app_role:
        raise RuntimeError(f"SET ROLE landed on {current!r}, expected {app_role!r}")
    conn.execute(sql.SQL("{}").format(sql.SQL(rendered)))  # pyright: ignore[reportArgumentType]  # psycopg wants LiteralString for raw SQL; the guarded migration text is a repo-owned constant
    print(f"guarded migration SQL re-run under SET ROLE {app_role} (session_user={session_user}): OK")  # noqa: T201


def _verify_catalog(conn: psycopg.Connection, schema: str, app_role: str) -> None:
    """Post-state catalog verification, run on the superuser connection after commit.

    Ownership and shape are proven here, BEFORE any read: a permission or
    undefined-column problem is caught without executing a query against the
    view's data.
    """
    for name in SPEND_VIEW_NAMES:
        owner, relkind, _, _ = _view_state(conn, schema, name)
        if owner != app_role:
            raise RuntimeError(f"view {schema}.{name} owned by {owner!r}, expected {app_role!r}")
        if relkind != "v":
            raise RuntimeError(f"view {schema}.{name} has relkind {relkind!r}")
    tag_columns: list[tuple[object, ...]] = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = %s AND table_name = 'DailyTagSpend'
        ORDER BY ordinal_position
        """,
        (schema,),
    ).fetchall()
    actual: list[str] = [str(c[0]) for c in tag_columns]
    expected: list[str] = ["individual_request_tag", "spend_date", "log_count", "total_spend"]
    if actual != expected:
        raise RuntimeError(f"DailyTagSpend columns {actual} do not match the 4-column contract {expected}")


def _verify_reads_as_app_role(conn: psycopg.Connection, schema: str, app_role: str) -> None:
    """Live read proof on a connection that is NOT the repair transaction's session.

    Runs under SET ROLE on the caller-provided connection, which the caller opens
    separately from the committed repair, in AUTOCOMMIT mode: a failed probe then
    aborts only its own implicit transaction and RESET ROLE in the finally block
    still runs, instead of dying on a poisoned multi-statement transaction.
    Reads are bounded at any data scale: every probe is ``LIMIT 0``, which still
    executes the full parse/plan/ACL path (a revoked grant fails the probe) but
    pulls zero rows and never scans the underlying logs. The tag view is probed
    with the same LIMIT 0 inside a checked narrow date window, never as an
    unbounded scan of retained tag history.
    """
    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(app_role)))
    try:
        current_row: tuple[object, ...] | None = conn.execute("SELECT current_user").fetchone()
        current: str = str(current_row[0]) if current_row else ""
        if current != app_role:
            raise RuntimeError(f"SET ROLE landed on {current!r}, expected {app_role!r}")
        window_start: str = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        window_end: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for name in SPEND_VIEW_NAMES:
            if name == _TAG_VIEW:
                conn.execute(
                    sql.SQL(
                        "SELECT individual_request_tag, spend_date, log_count, total_spend "
                        "FROM {}.{} WHERE spend_date >= %s::date AND spend_date <= %s::date LIMIT 0"
                    ).format(sql.Identifier(schema), sql.Identifier(name)),
                    (window_start, window_end),
                )
            else:
                conn.execute(
                    sql.SQL("SELECT 1 FROM {}.{} LIMIT 0").format(sql.Identifier(schema), sql.Identifier(name))
                )
        print(  # noqa: T201
            f"all seven views readable as {current}; every probe was LIMIT 0 (zero rows "
            f"pulled), tag view inside the {window_start}..{window_end} window"
        )
    finally:
        conn.execute("RESET ROLE")


class _ParsedArgs(LiteLLMBaseModel):
    superuser_url: str
    app_role: str
    schema_name: str
    gateway_url_env: str | None
    snapshot_out: str


class _RepairArgs(NamedTuple):
    superuser_url: str
    app_role: str
    schema: str
    gateway_url_env: str | None
    snapshot_out: str


def main(argv: Sequence[str] | None = None) -> int:
    description: str = __doc__ or "repair legacy spend view ownership"
    parser: Final = argparse.ArgumentParser(description=description.splitlines()[0])
    parser.add_argument("--superuser-url", required=True, help="superuser connection target (never logged)")
    parser.add_argument("--app-role", required=True, help="role the gateway connects as, e.g. litellm")
    parser.add_argument("--schema", default="public")
    parser.add_argument(
        "--gateway-url-env",
        default=None,
        help="environment variable holding the gateway DATABASE_URL; its role is cross-checked in memory",
    )
    parser.add_argument(
        "--snapshot-out",
        default="spend-view-repair-snapshot.json",
        help="local file for the pre-repair view snapshot",
    )
    raw: Final = parser.parse_args(argv)
    typed: Final = _ParsedArgs.model_validate(
        {
            "superuser_url": getattr(raw, "superuser_url"),
            "app_role": getattr(raw, "app_role"),
            "schema_name": getattr(raw, "schema"),
            "gateway_url_env": getattr(raw, "gateway_url_env") or None,
            "snapshot_out": getattr(raw, "snapshot_out"),
        },
    )
    args: Final = _RepairArgs(
        superuser_url=typed.superuser_url,
        app_role=typed.app_role,
        schema=typed.schema_name,
        gateway_url_env=typed.gateway_url_env,
        snapshot_out=typed.snapshot_out,
    )

    gateway_role: str | None = None
    if args.gateway_url_env:
        gateway_url: str | None = os.environ.get(args.gateway_url_env)
        if gateway_url is None:
            raise RuntimeError(f"environment variable {args.gateway_url_env} is not set")
        gateway_role = (urlparse(gateway_url).username or "").split(":")[0] or None

    with psycopg.connect(args.superuser_url, connect_timeout=10) as conn:
        conn.execute("SET lock_timeout = '10s'")
        conn.execute("SET idle_in_transaction_session_timeout = '30s'")
        conn.execute("SET statement_timeout = '120s'")
        _precheck(conn, args.schema, args.app_role, gateway_role)
        _snapshot(conn, args.schema, Path(args.snapshot_out))
        grants: Final[dict[str, tuple[tuple[str, str], ...]]] = {
            name: tuple(_reader_grants(conn, args.schema, name, owner))
            for name in SPEND_VIEW_NAMES
            if (owner := _view_state(conn, args.schema, name)[0]) != ""
        }
        try:
            with conn.transaction():
                _repair(conn, args.schema, args.app_role)
                _run_guarded_migration_as_app_role(conn, args.app_role)
                _restore_reader_grants(conn, args.schema, grants)
        except Exception:
            print(  # noqa: T201
                "repair transaction rolled back; no view definitions or ownership were changed"
            )
            raise
    print("repair transaction committed")  # noqa: T201

    try:
        with psycopg.connect(args.superuser_url, connect_timeout=10, autocommit=True) as verify_conn:
            verify_conn.execute("SET statement_timeout = '30s'")
            _verify_catalog(verify_conn, args.schema, args.app_role)
            _verify_reads_as_app_role(verify_conn, args.schema, args.app_role)
    except Exception as exc:
        print(  # noqa: T201
            f"VERIFICATION FAILED AFTER COMMIT: the repair DDL committed successfully, "
            f"but post-state verification failed: {exc}"
        )
        raise
    print("repair complete; the next prisma migrate deploy will now converge these views")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
