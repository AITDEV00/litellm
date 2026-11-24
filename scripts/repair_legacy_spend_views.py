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
2. prechecks the target role exists, is not a superuser-borrowed role, and
   matches the role the gateway actually connects as;
3. snapshots each view's owner, relkind, ACL and definition to a JSON file on
   the operator's machine (never inside the database, never with credentials);
4. in one bounded transaction: drops and recreates ``DailyTagSpend`` (legacy
   three-column shape), CREATE OR REPLACEs the other six from the single
   authoritative source (``litellm/proxy/db/spend_view_definitions.py``), and
   reassigns ownership of all seven to the application role;
5. re-runs the exact guarded migration SQL under ``SET ROLE <app role>`` in the
   same session, proving the application role can now converge the views
   itself, and asserts ``current_user`` at each stage;
6. verifies each view is readable and correctly shaped **as the application
   role**, not merely via a superuser privilege helper.

Fails closed on any precondition: unknown role, mismatched gateway role,
non-view object, unexpected schema, or a lock that cannot be obtained within
the bounded wait. Never prints connection strings or credentials.

Usage (from the repo root, with superuser access to the target database):

    python scripts/repair_legacy_spend_views.py \
        --superuser-url postgresql://postgres@127.0.0.1:15433/litellm \
        --app-role litellm \
        --gateway-url-env DATABASE_URL   # optional cross-check, value only read in memory

The superuser connection is operator-supplied (CNPG pod trust auth or a
port-forward with the superuser secret). The app-role password is never needed:
ownership changes and grants run as the superuser and the final readability
proof runs under ``SET ROLE``, which requires only membership, not the role's
password.
"""

import argparse
import json
import os
import sys
from collections.abc import Sequence
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


def _snapshot(conn: psycopg.Connection, schema: str, out_path: Path) -> None:
    states = {name: dict(zip(_SNAPSHOT_COLUMNS, _view_state(conn, schema, name))) for name in SPEND_VIEW_NAMES}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(states, indent=2, sort_keys=True))
    print(f"pre-repair snapshot written to {out_path} (no credentials included)")  # noqa: T201


def _repair(conn: psycopg.Connection, schema: str, app_role: str) -> None:
    for name in SPEND_VIEW_NAMES:
        target = sql.Identifier(schema, name)
        conn.execute(sql.SQL("DROP VIEW IF EXISTS {}").format(target))
        conn.execute(sql.SQL("{}").format(sql.SQL(spend_view_ddl(name))))  # pyright: ignore[reportArgumentType]  # psycopg wants LiteralString for raw SQL; the DDL is a repo-owned constant, not user input
        conn.execute(sql.SQL("ALTER VIEW {} OWNER TO {}").format(target, sql.Identifier(app_role)))
        print(f"recreated {schema}.{name}, owner set to {app_role}")  # noqa: T201


def _run_guarded_migration_as_app_role(conn: psycopg.Connection, app_role: str) -> None:
    rendered: str = render_migration_sql()
    if not MIGRATION_PATH.is_file() or MIGRATION_PATH.read_text() != rendered:
        raise RuntimeError("the shipped migration.sql does not match the authoritative view definitions")
    session_user_row: tuple[object, ...] | None = conn.execute("SELECT session_user").fetchone()
    session_user: str = str(session_user_row[0]) if session_user_row else ""
    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(app_role)))
    try:
        current_row: tuple[object, ...] | None = conn.execute("SELECT current_user").fetchone()
        current: str = str(current_row[0]) if current_row else ""
        if current != app_role:
            raise RuntimeError(f"SET ROLE landed on {current!r}, expected {app_role!r}")
        conn.execute(sql.SQL("{}").format(sql.SQL(rendered)))  # pyright: ignore[reportArgumentType]  # psycopg wants LiteralString for raw SQL; the guarded migration text is a repo-owned constant
        print(f"guarded migration SQL re-run under SET ROLE {app_role} (session_user={session_user}): OK")  # noqa: T201
    finally:
        conn.execute("RESET ROLE")


def _verify_as_app_role(conn: psycopg.Connection, schema: str, app_role: str) -> None:
    conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(app_role)))
    try:
        current_row: tuple[object, ...] | None = conn.execute("SELECT current_user").fetchone()
        current: str = str(current_row[0]) if current_row else ""
        for name in SPEND_VIEW_NAMES:
            owner, relkind, acl, _ = _view_state(conn, schema, name)
            if owner != app_role:
                raise RuntimeError(f"view {schema}.{name} owned by {owner!r}, expected {app_role!r}")
            if relkind != "v":
                raise RuntimeError(f"view {schema}.{name} has relkind {relkind!r}")
            if acl is not None:
                raise RuntimeError(
                    f"view {schema}.{name} carries a non-default ACL {acl!r}; the intended state is relacl IS NULL"
                )
            conn.execute(sql.SQL("SELECT 1 FROM {}.{} LIMIT 1").format(sql.Identifier(schema), sql.Identifier(name)))
        conn.execute(
            sql.SQL("SELECT individual_request_tag, spend_date, log_count, total_spend FROM {}.{} LIMIT 1").format(
                sql.Identifier(schema), sql.Identifier("DailyTagSpend")
            )
        )
        print(f"all seven views readable as {current}; DailyTagSpend exposes the 4-column contract")  # noqa: T201
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
        with conn.transaction():
            _repair(conn, args.schema, args.app_role)
        _run_guarded_migration_as_app_role(conn, args.app_role)
        _verify_as_app_role(conn, args.schema, args.app_role)
    print("repair complete; the next prisma migrate deploy will now converge these views")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
