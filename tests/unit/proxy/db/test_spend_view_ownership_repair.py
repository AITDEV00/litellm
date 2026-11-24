"""Real-Postgres regression tests for the spend-view ownership repair.

These prove the negative and positive behaviors the plan demands, against actual
database privileges and view shapes rather than mocked call counts:

* the guarded migration creates all seven views with the default ACL under the
  application role on a clean schema;
* the guarded migration refuses a foreign-owned view (even when the session user
  is a superuser, so no opportunistic SET ROLE/borrow can sneak in);
* the guarded migration refuses a same-named non-view object;
* a same-owner legacy 3-column ``DailyTagSpend`` converges to the 4-column
  contract via the guarded drop/recreate;
* the operator repair script converges the legacy foreign-owned state and its
  final proof is a real SELECT as the application role, not a superuser
  privilege helper;
* the empty-ACL trap the plan calls out: a view whose relacl is ``{}`` (what a
  mistaken blanket REVOKE leaves behind) denies its own owner, so the repair's
  snapshot captures ACLs and the desired end state is ``relacl IS NULL``.

The suite is skipped unless a disposable Postgres is reachable at
``SPEND_VIEW_TEST_PG_URL`` (application role) and ``SPEND_VIEW_TEST_PG_SUPERUSER_URL``
(superuser). CI runs them against the ephemeral container; locally:

    docker run -d --name spend-view-test-pg -e POSTGRES_PASSWORD=testpass \\
        -e POSTGRES_USER=postgres -p 15433:5432 postgres:16-alpine
    docker exec spend-view-test-pg psql -U postgres -c "CREATE ROLE litellm LOGIN PASSWORD 'litellm'" \\
        -c "CREATE DATABASE litellm OWNER litellm"
    docker exec spend-view-test-pg psql -U postgres -d litellm -c "GRANT ALL ON SCHEMA public TO litellm"
    docker exec spend-view-test-pg psql -U postgres -c "ALTER ROLE postgres PASSWORD 'pgtest'"
    export SPEND_VIEW_TEST_PG_URL=postgresql://litellm:litellm@127.0.0.1:15433/litellm
    export SPEND_VIEW_TEST_PG_SUPERUSER_URL=postgresql://postgres:pgtest@127.0.0.1:15433/litellm
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from litellm.proxy.db.spend_view_definitions import SPEND_VIEW_NAMES, spend_view_ddl  # noqa: E402
from scripts.render_spend_view_migration import render_migration_sql  # noqa: E402

REPO_ROOT: Path = Path(__file__).resolve().parents[4]

_APP_URL: str | None = os.environ.get("SPEND_VIEW_TEST_PG_URL")
_SUPER_URL: str | None = os.environ.get("SPEND_VIEW_TEST_PG_SUPERUSER_URL")

pytestmark = pytest.mark.skipif(
    _APP_URL is None or _SUPER_URL is None,
    reason="disposable Postgres fixture not configured (SPEND_VIEW_TEST_PG_URL / SPEND_VIEW_TEST_PG_SUPERUSER_URL)",
)

_DEPENDENT_TABLES: str = """
CREATE TABLE IF NOT EXISTS "LiteLLM_SpendLogs" (
    "startTime" TIMESTAMP NOT NULL,
    "spend" DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    "model" TEXT NOT NULL DEFAULT '',
    "api_key" TEXT NOT NULL DEFAULT '',
    "user" TEXT,
    "end_user" TEXT,
    "request_tags" JSONB
);
CREATE TABLE IF NOT EXISTS "LiteLLM_VerificationToken" (
    "token" TEXT PRIMARY KEY,
    "key_alias" TEXT,
    "key_name" TEXT
);
"""


@pytest.fixture()
def clean_schema() -> Iterator[psycopg.Connection]:
    """A connection as the application role over a schema with only the dependent tables."""
    with psycopg.connect(_APP_URL, connect_timeout=10, autocommit=True) as conn:
        _drop_named_objects(conn)
        conn.execute(_DEPENDENT_TABLES)
        yield conn
        _drop_named_objects(conn)


def _apply_migration(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(render_migration_sql())


def _drop_named_objects(conn: psycopg.Connection) -> None:
    """Remove any same-named leftover, whatever its relkind: DROP VIEW IF EXISTS
    still raises WrongObjectType when the name is held by a table, and vice versa."""
    for name, relkind in conn.execute(
        "SELECT c.relname, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = ANY(%s)",
        (list(SPEND_VIEW_NAMES),),
    ).fetchall():
        conn.execute(f'DROP {"VIEW" if relkind == "v" else "TABLE"} "{name}"')


def _view_state(conn: psycopg.Connection, name: str) -> tuple[str, str, str | None]:
    row = conn.execute(
        """
        SELECT pg_get_userbyid(c.relowner), c.relkind, c.relacl::text
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema() AND c.relname = %s
        """,
        (name,),
    ).fetchone()
    return (row[0], row[1], row[2]) if row else ("", "", None)


class TestGuardedMigrationOnCleanSchema:
    def test_creates_all_seven_views_owned_by_the_application_role_with_default_acl(self, clean_schema):
        _apply_migration(clean_schema)
        current: str = clean_schema.execute("SELECT current_user").fetchone()[0]
        for name in SPEND_VIEW_NAMES:
            owner, relkind, acl = _view_state(clean_schema, name)
            assert owner == current, f"{name} owned by {owner}, expected {current}"
            assert relkind == "v"
            assert acl is None, f"{name} must keep the default ACL (relacl IS NULL), got {acl}"
            clean_schema.execute(f'SELECT 1 FROM "{name}" LIMIT 1')

    def test_is_idempotent_when_rerun(self, clean_schema):
        _apply_migration(clean_schema)
        _apply_migration(clean_schema)
        current: str = clean_schema.execute("SELECT current_user").fetchone()[0]
        for name in SPEND_VIEW_NAMES:
            owner, relkind, _ = _view_state(clean_schema, name)
            assert owner == current
            assert relkind == "v"


class TestGuardedMigrationRefusals:
    def test_refuses_a_foreign_owned_view_as_the_application_role(self, clean_schema):
        clean_schema.execute('DROP VIEW IF EXISTS "Last30dModelsBySpend"')
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            super_conn.execute(spend_view_ddl("Last30dModelsBySpend"))
        with pytest.raises(psycopg.errors.RaiseException, match="refusing to borrow another owner"):
            _apply_migration(clean_schema)
        owner, relkind, _ = _view_state(clean_schema, "Last30dModelsBySpend")
        assert (owner, relkind) == ("postgres", "v"), "the refused migration must leave the object untouched"

    def test_refuses_a_foreign_owned_view_even_from_a_superuser_session(self, clean_schema):
        """A superuser could borrow the role; the guard must not let it. This pins the
        no-privilege-escalation property: ownership is checked, not privilege."""
        clean_schema.execute('DROP VIEW IF EXISTS "DailyTagSpend"')
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            super_conn.execute(spend_view_ddl("DailyTagSpend"))
            with pytest.raises(psycopg.errors.RaiseException, match="refusing to borrow another owner"):
                with super_conn.transaction():
                    super_conn.execute("SET LOCAL ROLE litellm")
                    super_conn.execute(render_migration_sql())

    def test_refuses_a_same_named_table(self, clean_schema):
        clean_schema.execute('DROP VIEW IF EXISTS "Last30dKeysBySpend"')
        clean_schema.execute('CREATE TABLE "Last30dKeysBySpend" (x int)')
        with pytest.raises(psycopg.errors.RaiseException, match="expected a view"):
            _apply_migration(clean_schema)
        owner, relkind, _ = _view_state(clean_schema, "Last30dKeysBySpend")
        assert relkind == "r"

    def test_converges_a_same_owner_legacy_tag_view_shape(self, clean_schema):
        """The legacy 3-column DailyTagSpend cannot be CREATE OR REPLACE'd (Postgres
        rejects adding columns), so the guarded block must drop and recreate it."""
        clean_schema.execute(
            """
            CREATE OR REPLACE VIEW "DailyTagSpend" AS
            SELECT jsonb_array_elements_text(request_tags) AS individual_request_tag,
                   SUM(spend) AS total_spend
            FROM "LiteLLM_SpendLogs"
            GROUP BY individual_request_tag;
            """
        )
        _apply_migration(clean_schema)
        columns = clean_schema.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'DailyTagSpend' ORDER BY ordinal_position
            """
        ).fetchall()
        assert [c[0] for c in columns] == ["individual_request_tag", "spend_date", "log_count", "total_spend"]


class TestEmptyAclTrap:
    def test_an_empty_acl_denies_even_the_owner(self, clean_schema):
        """The plan's trap: `REVOKE ALL ... FROM <owner>` leaves relacl `{}` and the
        owner loses implicit access. The migration recreating a same-owner view does
        not clear that, so the intended end state must remain relacl IS NULL and this
        test documents the failure mode a blanket revoke would create."""
        _apply_migration(clean_schema)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            clean_schema.execute('REVOKE ALL ON "MonthlyGlobalSpend" FROM CURRENT_USER')
            clean_schema.execute('SELECT 1 FROM "MonthlyGlobalSpend" LIMIT 1')


class TestOperatorRepair:
    def test_repairs_the_legacy_foreign_owned_state_and_proves_read_as_app_role(
        self, clean_schema, tmp_path, monkeypatch
    ):
        clean_schema.execute('DROP VIEW IF EXISTS "Last30dModelsBySpend"')
        clean_schema.execute('DROP VIEW IF EXISTS "DailyTagSpend"')
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            super_conn.execute(spend_view_ddl("Last30dModelsBySpend"))
            super_conn.execute(
                """
                CREATE VIEW "DailyTagSpend" AS
                SELECT jsonb_array_elements_text(request_tags) AS individual_request_tag,
                       SUM(spend) AS total_spend
                FROM "LiteLLM_SpendLogs"
                GROUP BY individual_request_tag;
                """
            )
        import subprocess
        import sys

        app_role: str = clean_schema.execute("SELECT current_user").fetchone()[0]
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(REPO_ROOT / "scripts" / "repair_legacy_spend_views.py"),
                "--superuser-url",
                _SUPER_URL,
                "--app-role",
                app_role,
                "--snapshot-out",
                str(tmp_path / "snapshot.json"),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0, result.stderr
        snapshot = (tmp_path / "snapshot.json").read_text()
        assert "postgres" in snapshot, "the pre-repair snapshot must record the foreign owner"
        for name in SPEND_VIEW_NAMES:
            owner, relkind, acl = _view_state(clean_schema, name)
            assert owner == app_role
            assert relkind == "v"
        clean_schema.execute(
            'SELECT individual_request_tag, spend_date, log_count, total_spend FROM "DailyTagSpend" LIMIT 1'
        )

    def test_repair_preserves_reader_grants_on_foreign_owned_views(self, clean_schema, tmp_path):
        """An operator-installed reader grant (e.g. a dashboard role) must survive the
        repair: the six replaced views and the recreated tag view get the recorded
        grants re-issued, so the repair fixes ownership without stripping readers."""
        import subprocess
        import sys

        _apply_migration(clean_schema)
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            super_conn.execute("DROP ROLE IF EXISTS dashboard_reader")
            super_conn.execute("CREATE ROLE dashboard_reader NOLOGIN")
            super_conn.execute('GRANT SELECT ON "Last30dModelsBySpend" TO dashboard_reader')
            super_conn.execute('GRANT SELECT ON "DailyTagSpend" TO dashboard_reader')
        try:
            app_role: str = clean_schema.execute("SELECT current_user").fetchone()[0]
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(REPO_ROOT / "scripts" / "repair_legacy_spend_views.py"),
                    "--superuser-url",
                    _SUPER_URL,
                    "--app-role",
                    app_role,
                    "--snapshot-out",
                    str(tmp_path / "snapshot.json"),
                ],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
            )
            assert result.returncode == 0, result.stderr
            for name in ("Last30dModelsBySpend", "DailyTagSpend"):
                grants = clean_schema.execute(
                    """
                    SELECT pg_get_userbyid(a.grantee), a.privilege_type
                    FROM pg_class c
                    CROSS JOIN LATERAL aclexplode(c.relacl) AS a
                    WHERE c.relname = %s AND pg_get_userbyid(a.grantee) = 'dashboard_reader'
                    """,
                    (name,),
                ).fetchall()
                assert grants, f"{name} lost its dashboard_reader grant"
                assert ("dashboard_reader", "SELECT") in [(str(g[0]), str(g[1])) for g in grants]
        finally:
            with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
                super_conn.execute('REVOKE ALL ON "Last30dModelsBySpend" FROM dashboard_reader')
                super_conn.execute('REVOKE ALL ON "DailyTagSpend" FROM dashboard_reader')
                super_conn.execute("DROP ROLE IF EXISTS dashboard_reader")

    def test_repair_refuses_a_view_with_dependent_objects(self, clean_schema, tmp_path):
        """A materialized view over DailyTagSpend is a dependent the drop would have to
        cascade to; the repair never cascades, so it must refuse before any DDL."""
        import subprocess
        import sys

        clean_schema.execute('DROP VIEW IF EXISTS "DailyTagSpend"')
        clean_schema.execute(
            """
            CREATE VIEW "DailyTagSpend" AS
            SELECT jsonb_array_elements_text(request_tags) AS individual_request_tag,
                   SUM(spend) AS total_spend
            FROM "LiteLLM_SpendLogs"
            GROUP BY individual_request_tag;
            """
        )
        clean_schema.execute('CREATE MATERIALIZED VIEW "tag_mv" AS SELECT * FROM "DailyTagSpend"')
        app_role: str = clean_schema.execute("SELECT current_user").fetchone()[0]
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(REPO_ROOT / "scripts" / "repair_legacy_spend_views.py"),
                "--superuser-url",
                _SUPER_URL,
                "--app-role",
                app_role,
                "--snapshot-out",
                str(tmp_path / "snapshot.json"),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
        assert "dependent objects" in (result.stderr + result.stdout)
        owner, _, _ = _view_state(clean_schema, "DailyTagSpend")
        assert owner == app_role, "the refused repair must leave the view untouched"
        clean_schema.execute('DROP MATERIALIZED VIEW "tag_mv"')

    def test_failed_verification_reports_committed_ddl(self, clean_schema, tmp_path):
        """If the atomic DDL transaction commits but post-state verification fails,
        the script must say the DDL committed rather than claiming total failure."""
        import subprocess
        import sys

        app_role: str = clean_schema.execute("SELECT current_user").fetchone()[0]
        runner = (
            "import sys; sys.argv = ['repair', '--superuser-url', %r, '--app-role', %r, "
            "'--snapshot-out', %r];\n"
            "sys.path.insert(0, %r);\n"
            "import scripts.repair_legacy_spend_views as m;\n"
            "calls = [];\n"
            "orig = m._verify_catalog;\n"
            "def boom(conn, schema, app_role):\n"
            "    calls.append(1);\n"
            "    raise RuntimeError('post-state ownership mismatch');\n"
            "m._verify_catalog = boom;\n"
            "raise SystemExit(m.main())\n"
        ) % (_SUPER_URL, app_role, str(tmp_path / "snapshot.json"), str(REPO_ROOT))
        result = subprocess.run(  # test-quality-ok: TQ009 the injection harness must import scripts.* from the checkout to monkeypatch the repair module; -I would defeat that, and the subprocess runs the repo's own code, not an installed package
            [sys.executable, "-c", runner],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "VERIFICATION FAILED AFTER COMMIT" in combined
        assert "repair transaction committed" in combined
        for name in SPEND_VIEW_NAMES:
            owner, relkind, _ = _view_state(clean_schema, name)
            assert owner == app_role, f"DDL must have committed for {name}"
            assert relkind == "v"

    def test_ddl_failure_rolls_back_the_whole_transaction(self, clean_schema, tmp_path):
        """A failure inside the DDL transaction (here: a dependency error raised
        mid-repair) must roll back every view change, leaving the legacy state."""
        import subprocess
        import sys

        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            super_conn.execute(spend_view_ddl("Last30dModelsBySpend"))
        app_role: str = clean_schema.execute("SELECT current_user").fetchone()[0]
        runner = (
            "import sys; sys.argv = ['repair', '--superuser-url', %r, '--app-role', %r, "
            "'--snapshot-out', %r];\n"
            "sys.path.insert(0, %r);\n"
            "import scripts.repair_legacy_spend_views as m;\n"
            "orig = m._repair;\n"
            "def boom(conn, schema, app_role):\n"
            "    orig(conn, schema, app_role);\n"
            "    raise RuntimeError('injected mid-repair failure')\n"
            "m._repair = boom;\n"
            "raise SystemExit(m.main())\n"
        ) % (_SUPER_URL, app_role, str(tmp_path / "snapshot.json"), str(REPO_ROOT))
        result = subprocess.run(  # test-quality-ok: TQ009 same injection harness as above: it must import the repo's scripts package to monkeypatch _repair, which needs the checkout on sys.path
            [sys.executable, "-c", runner],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "injected mid-repair failure" in combined, (
            "the failure must come from the injected post-DDL raise, not a harness TypeError"
        )
        assert "rolled back" in combined
        owner, _, _ = _view_state(clean_schema, "Last30dModelsBySpend")
        assert owner == "postgres", "rollback must restore the foreign owner"

    def test_repair_refuses_when_not_run_as_superuser(self, clean_schema, tmp_path):
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(REPO_ROOT / "scripts" / "repair_legacy_spend_views.py"),
                "--superuser-url",
                _APP_URL,
                "--app-role",
                "litellm",
                "--snapshot-out",
                str(tmp_path / "snapshot.json"),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
        assert "superuser" in (result.stderr + result.stdout).lower()
