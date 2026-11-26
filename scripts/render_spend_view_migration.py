"""Render the guarded spend-view Prisma migration from the authoritative module.

Usage::

    python scripts/render_spend_view_migration.py [--check]

Writes ``litellm-proxy-extras/litellm_proxy_extras/migrations/20261124120000_spend_view_definitions_and_ownership/migration.sql``.
With ``--check`` the rendered text is compared against the file on disk and the
process exits non-zero on drift, so CI (and ``make check`` via the mapped unit
test) catches a definition edit that missed the regeneration step.

The migration's SQL body is generated here, not hand-maintained, so the
migration, the boot bootstrap, and the operator repair script all render the
exact same view DDL from ``litellm/proxy/db/spend_view_definitions.py``.
"""

import sys
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from litellm.proxy.db.spend_view_definitions import (  # noqa: E402
    SPEND_VIEW_DDL,
    SPEND_VIEW_NAMES,
    SpendViewDefinition,
    spend_view_ddl,
)

MIGRATION_DIRNAME: Final = "20261124120000_spend_view_definitions_and_ownership"
MIGRATION_PATH: Final = (
    REPO_ROOT / "litellm-proxy-extras" / "litellm_proxy_extras" / "migrations" / MIGRATION_DIRNAME / "migration.sql"
)

_HEADER: Final = """-- Regenerate with: python scripts/render_spend_view_migration.py
-- Source of truth: litellm/proxy/db/spend_view_definitions.py
--
-- Defines the seven spend aggregation views and refuses to touch an object the
-- application database role does not already own. View DDL and metadata only:
-- no row rewrites, no ACL mutation, no SET ROLE.
SET LOCAL lock_timeout = '10s';
SET LOCAL idle_in_transaction_session_timeout = '5s';

DO $$
DECLARE
    view_name text;
    object_owner text;
    object_kind "char";
    current_role text := current_user;
BEGIN
"""

_FOOTER: Final = """END
$$;
"""


def _render_view_block(definition: SpendViewDefinition) -> str:
    ddl = spend_view_ddl(definition.name).strip()
    quoted = definition.name.replace('"', '""')
    return f"""    -- {definition.name}: correct when absent or already owned by current_user
    view_name := '{quoted}';
    SELECT pg_get_userbyid(c.relowner), c.relkind
      INTO object_owner, object_kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relname = view_name;
    IF object_kind IS NULL THEN
{ddl}
    ELSIF object_kind <> 'v' THEN
        RAISE EXCEPTION
            'spend view % is a relkind %s object, expected a view (relkind v); refusing to alter it',
            view_name, object_kind;
    ELSIF object_owner <> current_role THEN
        RAISE EXCEPTION
            'spend view % is owned by %, expected %; refusing to borrow another owner. '
            'Run the operator repair workflow (oicm-litellm-layer/docs/operations/runbooks/spend-view-legacy-repair.md) as the database superuser, then re-run migrations',
            view_name, object_owner, current_role;
    END IF;
"""


def render_migration_sql() -> str:
    blocks: list[str] = []
    for name in SPEND_VIEW_NAMES:
        definition: SpendViewDefinition = SPEND_VIEW_DDL[name]
        if definition.allow_replace:
            blocks.append(_render_view_block(definition))
        else:
            blocks.append(_render_drop_recreate_block(definition))
    return _HEADER + "".join(blocks) + _FOOTER


def _render_drop_recreate_block(definition: SpendViewDefinition) -> str:
    """DailyTagSpend's column contract changed (legacy 3 columns to 4).

    CREATE OR REPLACE VIEW cannot add columns to an existing view, so a
    same-owner legacy-shape view must be dropped and recreated inside the
    guarded ownership check.
    """
    ddl = spend_view_ddl(definition.name).strip()
    quoted = definition.name.replace('"', '""')
    return f"""    -- {definition.name}: drop/recreate when absent or owned by current_user
    view_name := '{quoted}';
    SELECT pg_get_userbyid(c.relowner), c.relkind
      INTO object_owner, object_kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = current_schema() AND c.relname = view_name;
    IF object_kind IS NULL THEN
{ddl}
    ELSIF object_kind <> 'v' THEN
        RAISE EXCEPTION
            'spend view % is a relkind %s object, expected a view (relkind v); refusing to alter it',
            view_name, object_kind;
    ELSIF object_owner <> current_role THEN
        RAISE EXCEPTION
            'spend view % is owned by %, expected %; refusing to borrow another owner. '
            'Run the operator repair workflow (oicm-litellm-layer/docs/operations/runbooks/spend-view-legacy-repair.md) as the database superuser, then re-run migrations',
            view_name, object_owner, current_role;
    ELSE
        DROP VIEW "DailyTagSpend";
{ddl}
    END IF;
"""


def main() -> int:
    rendered: str = render_migration_sql()
    if "--check" in sys.argv:
        if not MIGRATION_PATH.is_file():
            print(f"missing migration file: {MIGRATION_PATH}")  # noqa: T201
            return 1
        on_disk: str = MIGRATION_PATH.read_text()
        if on_disk != rendered:
            print(  # noqa: T201
                "migration SQL drift: rerun python scripts/render_spend_view_migration.py "
                "after editing litellm/proxy/db/spend_view_definitions.py"
            )
            return 1
        print("migration SQL matches the authoritative view definitions")  # noqa: T201
        return 0
    MIGRATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    MIGRATION_PATH.write_text(rendered)
    print(f"wrote {MIGRATION_PATH}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
