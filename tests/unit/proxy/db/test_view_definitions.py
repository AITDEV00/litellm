"""Drift guards for the single spend-view definition set.

Three surfaces must render the same seven view DDL: the boot bootstrap
(`litellm/proxy/db/create_views.py`), the operator script
(`db_scripts/create_views.py`), and the guarded Prisma migration. The migration
file is generated from `spend_view_definitions.py` by
`scripts/render_spend_view_migration.py`; these tests fail when any surface
stops rendering from the authoritative module or when a definition edit missed
the regeneration step.
"""

from pathlib import Path

import pytest

from litellm.proxy.db.spend_view_definitions import (
    SPEND_VIEW_DDL,
    SPEND_VIEW_NAMES,
    SpendViewDefinition,
    spend_view_ddl,
    spend_view_names,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[4]
MIGRATION_PATH: Path = (
    REPO_ROOT
    / "litellm-proxy-extras"
    / "litellm_proxy_extras"
    / "migrations"
    / "20261124120000_spend_view_definitions_and_ownership"
    / "migration.sql"
)

from scripts.render_spend_view_migration import render_migration_sql  # noqa: E402

EXPECTED_VIEWS: frozenset[str] = frozenset(
    {
        "MonthlyGlobalSpend",
        "Last30dKeysBySpend",
        "Last30dModelsBySpend",
        "MonthlyGlobalSpendPerKey",
        "MonthlyGlobalSpendPerUserPerKey",
        "DailyTagSpend",
        "Last30dTopEndUsersSpend",
    }
)


def test_the_seven_spend_views_are_exactly_the_expected_set():
    assert set(spend_view_names()) == EXPECTED_VIEWS
    assert len(SPEND_VIEW_NAMES) == len(EXPECTED_VIEWS)


def test_unknown_view_name_raises_instead_of_inventing_ddl():
    with pytest.raises(KeyError):
        spend_view_ddl("LiteLLM_VerificationTokenView")


def test_daily_tag_spend_is_the_only_drop_recreate_view():
    """The tag view's column contract changed, so it cannot be CREATE OR REPLACEd onto
    the legacy 3-column shape; every other view has a stable column list."""
    drop_recreate = {name for name in SPEND_VIEW_NAMES if not SPEND_VIEW_DDL[name].allow_replace}
    assert drop_recreate == {"DailyTagSpend"}


def test_migration_file_matches_the_authoritative_definitions():
    """The shipped migration.sql must be exactly what the renderer produces today.

    Failing here means someone edited a view definition (or the migration) without
    rerunning `python scripts/render_spend_view_migration.py`.
    """
    assert MIGRATION_PATH.is_file(), f"missing migration: {MIGRATION_PATH}"
    on_disk: str = MIGRATION_PATH.read_text()
    assert on_disk == render_migration_sql()


def test_migration_renders_every_view_and_no_others():
    rendered: str = render_migration_sql()
    for name in SPEND_VIEW_NAMES:
        definition: SpendViewDefinition = SPEND_VIEW_DDL[name]
        assert definition.ddl.strip() in rendered, f"{name} DDL missing from the migration"
    assert 'CREATE VIEW "LiteLLM_VerificationTokenView"' not in rendered


def test_migration_refuses_instead_of_borrowing_ownership():
    """The guarded block must fail-closed on foreign owners and non-view objects.

    These assertions pin the refusal semantics: the DO block compares the recorded
    owner against current_user and raises; it never SET ROLE and never repairs.
    """
    rendered: str = render_migration_sql()
    executable_lines: list[str] = [line for line in rendered.splitlines() if not line.lstrip().startswith("--")]
    import re

    assert not any(re.search(r"\bSET\s+ROLE\b", line) for line in executable_lines)
    assert "refusing to borrow another owner" in rendered
    assert "expected a view (relkind v)" in rendered
    assert "ALTER VIEW" not in rendered
    # The refusal message must name the operator path, so the on-call fixes the
    # environment instead of hunting through migration source.
    assert "spend-view-legacy-repair" in rendered


def test_migration_sets_bounded_locks_and_no_row_writes():
    rendered: str = render_migration_sql()
    assert "SET LOCAL lock_timeout" in rendered
    assert "SET LOCAL idle_in_transaction_session_timeout" in rendered
    forbidden = ("UPDATE ", "DELETE FROM", "INSERT INTO", "MERGE ")
    upper: str = rendered.upper()
    for statement in forbidden:
        assert statement not in upper, f"row-rewrite statement {statement!r} must never appear in the migration"


def test_last30_views_keep_their_30_day_predicates():
    """The model/key views are bounded 30-day reads; the repair must not rewrite them
    into unbounded scans."""
    models: str = spend_view_ddl("Last30dModelsBySpend")
    keys: str = spend_view_ddl("Last30dKeysBySpend")
    assert "CURRENT_DATE - INTERVAL '30 days'" in models
    assert "CURRENT_DATE - INTERVAL '30 days'" in keys
    assert "AND \"model\" != ''" in models


def test_daily_tag_spend_contract_has_four_columns():
    ddl: str = spend_view_ddl("DailyTagSpend")
    for column in ("individual_request_tag", "spend_date", "log_count", "total_spend"):
        assert column in ddl
