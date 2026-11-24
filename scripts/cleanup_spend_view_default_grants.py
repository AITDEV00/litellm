"""One-shot operator cleanup of the fork-local broad default grant for views.

Explicitly invoked by a database administrator; never run by the proxy, the
janitor, or any migration job. The defect this repairs: the prod janitor
bootstrap installed

    ALTER DEFAULT PRIVILEGES FOR ROLE litellm GRANT ALL ON TABLES TO oicm;

so every future table *and view* created by the application role hands the
janitor role full admin privileges. Views are relations of objtype ``r`` for
default-ACL purposes, which is why the repaired ``DailyTagSpend`` came back
from its guarded-migration recreate carrying ``oicm=arwdDxtm/litellm``. The
janitor needs none of that: it holds ``GRANT litellm TO oicm`` membership, and
membership already lets it read, dump, detach and drop partitions owned by
``litellm``.

What the cleanup does, in ONE transaction:

1. removes exactly the ``<creator>-grants-<grantee>`` relations (objtype ``r``)
   default ACL entry, leaving every other default ACL (including the separate
   ``postgres``-creator row that covers conversion-runbook partitions)
   untouched;
2. strips the already-materialized ``<grantee>`` ACL entries from the seven
   spend views when, and only when, they exactly match the default-ACL
   signature; any other grantee, privilege set, or grantor is a hard stop;
3. re-proves the end state: the default ACL entry is gone, no view carries a
   grantee entry, the application role still reads every view, and the
   grantee's membership survives.

Fails closed on any unexpected catalog state: a default ACL whose grantee
entry is narrower than the broad grant (so a revoke would silently drop a
deliberate narrow grant), non-view same-named objects, third-party view owners,
missing role membership, or a view ACL that does not match the default-grant
signature. Never prints connection strings or credentials.

Usage (from the repo root, with superuser access to the target database):

    # read-only: print the catalog state and the exact planned statements
    python scripts/cleanup_spend_view_default_grants.py \
        --superuser-url postgresql://postgres@127.0.0.1:15432/litellm \
        --app-role litellm --grantee-role oicm

    # the same run with the changes applied (explicit operator approval)
    python scripts/cleanup_spend_view_default_grants.py ... --apply
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import psycopg
from psycopg import sql

from litellm.types.llms.base import LiteLLMBaseModel

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from litellm.proxy.db.spend_view_definitions import SPEND_VIEW_NAMES  # noqa: E402

_BREADTH_REQUIRED: Final = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})


def _grantee_privileges(conn: psycopg.Connection, schema: str, name: str, grantee: str) -> list[str]:
    """Privileges the grantee holds via an explicit ACL entry on one relation.

    Membership-derived rights are deliberately not filtered out: a grantee that
    is also a member of the owner still has a real, revocable explicit entry,
    and that entry is exactly what the default grant materialized.
    """
    rows: list[tuple[object, ...]] = conn.execute(
        """
        SELECT a.privilege_type
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) AS a
        WHERE n.nspname = %s AND c.relname = %s
          AND pg_get_userbyid(a.grantee) = %s
        """,
        (schema, name, grantee),
    ).fetchall()
    return sorted(str(r[0]) for r in rows)


def _grantee_grantors(conn: psycopg.Connection, schema: str, name: str, grantee: str) -> list[str]:
    rows: list[tuple[object, ...]] = conn.execute(
        """
        SELECT DISTINCT pg_get_userbyid(a.grantor)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        CROSS JOIN LATERAL aclexplode(c.relacl) AS a
        WHERE n.nspname = %s AND c.relname = %s
          AND pg_get_userbyid(a.grantee) = %s
        """,
        (schema, name, grantee),
    ).fetchall()
    return sorted(str(r[0]) for r in rows)


def _default_acl_privileges(conn: psycopg.Connection, creator: str, grantee: str) -> list[str]:
    """Privileges the creator's relations default ACL grants to the grantee."""
    rows: list[tuple[object, ...]] = conn.execute(
        """
        SELECT a.privilege_type
        FROM pg_default_acl d
        JOIN pg_roles cr ON cr.oid = d.defaclrole
        CROSS JOIN LATERAL aclexplode(d.defaclacl) AS a
        JOIN pg_roles g ON g.oid = a.grantee
        WHERE d.defaclnamespace = 0
          AND d.defaclobjtype = 'r'
          AND cr.rolname = %s
          AND g.rolname = %s
        """,
        (creator, grantee),
    ).fetchall()
    return sorted(str(r[0]) for r in rows)


def _is_member_of(conn: psycopg.Connection, member: str, role: str) -> bool:
    row: tuple[object, ...] | None = conn.execute(
        """
        SELECT 1 FROM pg_auth_members am
        JOIN pg_roles g ON g.oid = am.roleid
        JOIN pg_roles m ON m.oid = am.member
        WHERE g.rolname = %s AND m.rolname = %s
        """,
        (role, member),
    ).fetchone()
    return row is not None


def _precheck(conn: psycopg.Connection, schema: str, app_role: str, grantee: str) -> None:
    db_row: tuple[object, ...] | None = conn.execute(
        "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    if db_row is None or not bool(db_row[1]):
        raise RuntimeError("cleanup must run as a database superuser (ALTER DEFAULT PRIVILEGES owner check)")
    for role, label in ((app_role, "creator"), (grantee, "grantee")):
        row: tuple[object, ...] | None = conn.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"{label} role {role!r} does not exist")
        if bool(row[0]):
            raise RuntimeError(f"refusing to treat superuser role {role!r} as the {label}")
    if not conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)).fetchone():
        raise RuntimeError(f"schema {schema!r} does not exist")
    if not _is_member_of(conn, grantee, app_role):
        raise RuntimeError(
            f"expected membership {grantee!r} MEMBER OF {app_role!r} is missing; "
            "the cleanup preserves it, it does not create it"
        )
    for name in SPEND_VIEW_NAMES:
        state: tuple[str, str] = conn.execute(
            """
            SELECT pg_get_userbyid(c.relowner), c.relkind
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
            """,
            (schema, name),
        ).fetchone() or ("", "")
        owner, relkind = state
        if owner == "":
            continue
        if relkind != "v":
            raise RuntimeError(f"object {schema}.{name} is relkind {relkind!r}, not a view; refusing")
        if owner != app_role:
            raise RuntimeError(
                f"view {schema}.{name} is owned by {owner!r}, expected {app_role!r}; "
                "refusing to touch a third party's object"
            )


def _plan(conn: psycopg.Connection, schema: str, app_role: str, grantee: str) -> tuple[list[str], dict[str, list[str]]]:
    """Validate the catalog and return (default-ACL privileges, per-view entries)."""
    default_privileges: list[str] = _default_acl_privileges(conn, app_role, grantee)
    if default_privileges and not _BREADTH_REQUIRED.issubset(set(default_privileges)):
        raise RuntimeError(
            f"creator {app_role!r} default ACL grants {grantee!r} only {default_privileges}; "
            f"the broad-grant signature must include {sorted(_BREADTH_REQUIRED)}. "
            "This is not the reported broad default grant; refusing to guess, review manually"
        )
    view_plan: dict[str, list[str]] = {}
    for name in SPEND_VIEW_NAMES:
        entries: list[str] = _grantee_privileges(conn, schema, name, grantee)
        if not entries:
            continue
        if set(entries) != set(default_privileges):
            raise RuntimeError(
                f"view {schema}.{name} carries {grantee!r} privileges {entries}, but the "
                f"default ACL grants {default_privileges}; signature mismatch, refusing a partial revoke"
            )
        grantors: list[str] = _grantee_grantors(conn, schema, name, grantee)
        unexpected: list[str] = [g for g in grantors if g != app_role]
        if unexpected:
            raise RuntimeError(f"view {schema}.{name} grantee entries were granted by {unexpected}; refusing")
        view_plan[name] = entries
    return (default_privileges, view_plan)


class _ParsedArgs(LiteLLMBaseModel):
    superuser_url: str
    app_role: str
    grantee_role: str
    schema_name: str
    apply: bool


def main(argv: Sequence[str] | None = None) -> int:
    description: str = __doc__ or "cleanup broad default view grants"
    parser: Final = argparse.ArgumentParser(description=description.splitlines()[0])
    parser.add_argument("--superuser-url", required=True, help="superuser connection target (never logged)")
    parser.add_argument("--app-role", required=True, help="the application creator role, e.g. litellm")
    parser.add_argument("--grantee-role", required=True, help="the janitor role the default grant widens, e.g. oicm")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--apply", action="store_true", help="apply the planned cleanup (default: dry-run)")
    raw: Final = parser.parse_args(argv)
    typed: Final = _ParsedArgs.model_validate(
        {
            "superuser_url": getattr(raw, "superuser_url"),
            "app_role": getattr(raw, "app_role"),
            "grantee_role": getattr(raw, "grantee_role"),
            "schema_name": getattr(raw, "schema"),
            "apply": getattr(raw, "apply") is True,
        }
    )

    with psycopg.connect(typed.superuser_url, connect_timeout=10) as conn:
        conn.execute("SET lock_timeout = '10s'")
        conn.execute("SET idle_in_transaction_session_timeout = '30s'")
        conn.execute("SET statement_timeout = '60s'")
        _precheck(conn, typed.schema_name, typed.app_role, typed.grantee_role)
        default_privileges, view_plan = _plan(conn, typed.schema_name, typed.app_role, typed.grantee_role)

        mode: str = "DRY-RUN (no changes; pass --apply to execute)" if not typed.apply else "APPLY"
        print(f"{mode}: grantee {typed.grantee_role!r}, creator {typed.app_role!r}, schema {typed.schema_name!r}")  # noqa: T201
        if default_privileges:
            print(  # noqa: T201
                f"  1. ALTER DEFAULT PRIVILEGES FOR ROLE {typed.app_role} "
                f"REVOKE ALL ON TABLES FROM {typed.grantee_role};"
            )
            print(f"     removes the default ACL entry granting {default_privileges}")  # noqa: T201
        else:
            print("  1. no creator default ACL entry for this grantee; nothing to do")  # noqa: T201
        for name, entries in view_plan.items():
            print(f"  2. REVOKE ALL ON {typed.schema_name}.{name} FROM {typed.grantee_role};")  # noqa: T201
            print(f"     removes materialized entries {entries}")  # noqa: T201
        if not view_plan:
            print("  2. no view carries a materialized grantee entry; nothing to do")  # noqa: T201
        if not typed.apply:
            return 0

        try:
            with conn.transaction():
                if default_privileges:
                    conn.execute(
                        sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE ALL ON TABLES FROM {}").format(
                            sql.Identifier(typed.app_role), sql.Identifier(typed.grantee_role)
                        )
                    )
                for name in view_plan:
                    conn.execute(
                        sql.SQL("REVOKE ALL ON {}.{} FROM {}").format(
                            sql.Identifier(typed.schema_name),
                            sql.Identifier(name),
                            sql.Identifier(typed.grantee_role),
                        )
                    )
        except Exception:
            print("cleanup transaction rolled back; no default ACL or view grant was changed")  # noqa: T201
            raise
    print("cleanup transaction committed")  # noqa: T201

    with psycopg.connect(typed.superuser_url, connect_timeout=10, autocommit=True) as verify_conn:
        verify_conn.execute("SET statement_timeout = '30s'")
        if _default_acl_privileges(verify_conn, typed.app_role, typed.grantee_role):
            raise RuntimeError("the creator default ACL entry for the grantee is still present after cleanup")
        for name in SPEND_VIEW_NAMES:
            if _grantee_privileges(verify_conn, typed.schema_name, name, typed.grantee_role):
                raise RuntimeError(f"view {typed.schema_name}.{name} still carries a grantee entry after cleanup")
            exists: object = verify_conn.execute(
                "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s",
                (typed.schema_name, name),
            ).fetchone()
            if exists is None:
                continue
            readable_row: tuple[object, ...] | None = verify_conn.execute(
                "SELECT has_table_privilege(%s, %s, 'SELECT')",
                (typed.app_role, f'{typed.schema_name}."{name}"'),
            ).fetchone()
            if readable_row is None or not bool(readable_row[0]):
                raise RuntimeError(f"application role lost SELECT on {typed.schema_name}.{name}")
        if not _is_member_of(verify_conn, typed.grantee_role, typed.app_role):
            raise RuntimeError("grantee membership in the application role was lost")
    print(  # noqa: T201
        f"verified: default ACL entry gone, no grantee view entries, {typed.app_role} reads all views, "
        f"membership {typed.grantee_role} MEMBER OF {typed.app_role} preserved"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
