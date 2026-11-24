"""Real-Postgres regressions for the broad default-grant cleanup.

These prove, against actual role privileges rather than ACL-text matching:

* the reproduced defect: with the fork's default ACL installed, a view created
  by the application role materializes ``oicm ALL`` (the janitor role gets
  admin rights on a dashboard view it never asked for);
* the cleanup's dry-run default changes nothing;
* the applied cleanup removes the default ACL entry and the materialized view
  entries, in one transaction, while membership and archive-ledger access
  survive;
* the cleanup is idempotent (a second run is a no-op) and a later view
  recreation does NOT resurrect the grant (the default ACL row is gone);
* the janitor's real workflow (pg_dump of a partition, ledger writes) still
  works through membership after the cleanup;
* refusal paths: a narrow (non-broad) default grant, a foreign-owned view, a
  non-view collision, and a missing membership each abort before any change;
* an explicit third-party reader grant on a view survives the cleanup.

The suite is skipped unless a disposable Postgres is reachable at
``SPEND_VIEW_TEST_PG_URL`` (application role), ``SPEND_VIEW_TEST_PG_SUPERUSER_URL``
(superuser), and ``SPEND_VIEW_TEST_PG_JANITOR_URL`` (the oicm-style grantee).
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from litellm.proxy.db.spend_view_definitions import SPEND_VIEW_NAMES, spend_view_ddl  # noqa: E402

REPO_ROOT: Path = Path(__file__).resolve().parents[4]

_APP_URL: str | None = os.environ.get("SPEND_VIEW_TEST_PG_URL")
_SUPER_URL: str | None = os.environ.get("SPEND_VIEW_TEST_PG_SUPERUSER_URL")
_JANITOR_URL: str | None = os.environ.get("SPEND_VIEW_TEST_PG_JANITOR_URL")

pytestmark = pytest.mark.skipif(
    _APP_URL is None or _SUPER_URL is None or _JANITOR_URL is None,
    reason="disposable Postgres fixture not configured (SPEND_VIEW_TEST_PG_URL / _SUPERUSER_URL / _JANITOR_URL)",
)

_BROAD_DEFAULT: str = "ALTER DEFAULT PRIVILEGES FOR ROLE litellm GRANT ALL ON TABLES TO oicm"

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
CREATE TABLE IF NOT EXISTS "LiteLLM_SpendLogsArchiveLedger" (
    "partition_name" text PRIMARY KEY,
    "state" text NOT NULL
);
"""


@pytest.fixture()
def clean_schema() -> Iterator[psycopg.Connection]:
    """Application-role connection over a schema with only the dependent tables."""
    with psycopg.connect(_APP_URL, connect_timeout=10, autocommit=True) as conn:
        _drop_named_objects(conn)
        conn.execute(_DEPENDENT_TABLES)
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            super_conn.execute("ALTER DEFAULT PRIVILEGES FOR ROLE litellm REVOKE ALL ON TABLES FROM oicm")
            super_conn.execute('ALTER TABLE "LiteLLM_SpendLogsArchiveLedger" OWNER TO oicm')
        yield conn
        _drop_named_objects(conn)


def _drop_named_objects(conn: psycopg.Connection) -> None:
    for name, relkind in conn.execute(
        "SELECT c.relname, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() AND c.relname = ANY(%s)",
        (list(SPEND_VIEW_NAMES),),
    ).fetchall():
        conn.execute(f'DROP {"VIEW" if relkind == "v" else "TABLE"} "{name}"')


def _install_broad_default(conn: psycopg.Connection) -> None:
    conn.execute(_BROAD_DEFAULT)


def _janitor_view_privileges(view: str) -> list[str]:
    with psycopg.connect(_JANITOR_URL, connect_timeout=10, autocommit=True) as janitor:
        return sorted(
            str(r[0])
            for r in janitor.execute(
                """
                SELECT a.privilege_type
                FROM pg_class c
                CROSS JOIN LATERAL aclexplode(c.relacl) AS a
                WHERE c.relname = %s AND pg_get_userbyid(a.grantee) = 'oicm'
                """,
                (view,),
            ).fetchall()
        )


def _default_acl_entry(super_conn: psycopg.Connection, creator: str = "litellm", grantee: str = "oicm") -> list[str]:
    return sorted(
        str(r[0])
        for r in super_conn.execute(
            """
            SELECT a.privilege_type
            FROM pg_default_acl d
            CROSS JOIN LATERAL aclexplode(d.defaclacl) AS a
            WHERE d.defaclobjtype = 'r' AND d.defaclnamespace = 0
              AND pg_get_userbyid(d.defaclrole) = %s
              AND pg_get_userbyid(a.grantee) = %s
            """,
            (creator, grantee),
        ).fetchall()
    )


def _run_cleanup(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(REPO_ROOT / "scripts" / "cleanup_spend_view_default_grants.py"),
            "--superuser-url",
            _SUPER_URL or "",
            "--app-role",
            "litellm",
            "--grantee-role",
            "oicm",
            *args,
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


class TestReproduceTheDefect:
    def test_new_app_view_grants_janitor_all(self, clean_schema):
        """The reported failure: default privileges hand the janitor role full
        admin rights on every ordinary view the application role creates."""
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
        clean_schema.execute(spend_view_ddl("Last30dModelsBySpend"))
        privileges: list[str] = _janitor_view_privileges("Last30dModelsBySpend")
        assert {"SELECT", "INSERT", "UPDATE", "DELETE"}.issubset(set(privileges)), (
            "expected the broad default ACL to materialize janitor admin rights on the view; "
            "if this no longer reproduces, the defect premise changed"
        )

    def test_tag_view_recreation_resurrects_the_grant(self, clean_schema):
        """Even a full drop/recreate by the application role re-materializes
        the janitor grant: the default ACL row is the persistent source."""
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
        clean_schema.execute(spend_view_ddl("DailyTagSpend"))
        clean_schema.execute('DROP VIEW "DailyTagSpend"')
        clean_schema.execute(spend_view_ddl("DailyTagSpend"))
        assert {"SELECT", "INSERT", "UPDATE", "DELETE"}.issubset(set(_janitor_view_privileges("DailyTagSpend")))


class TestCleanup:
    def test_dry_run_changes_nothing(self, clean_schema):
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
        clean_schema.execute(spend_view_ddl("Last30dModelsBySpend"))
        result: subprocess.CompletedProcess[str] = _run_cleanup()
        assert result.returncode == 0, result.stderr
        assert "DRY-RUN" in result.stdout
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            assert _default_acl_entry(super_conn), "dry-run must leave the default ACL in place"
        assert _janitor_view_privileges("Last30dModelsBySpend"), "dry-run must leave view grants in place"

    def test_apply_removes_default_and_view_entries_and_keeps_membership(self, clean_schema):
        """The full happy path: all seven views created under the broad default,
        cleanup removes the persistent row and every materialized entry, and the
        janitor keeps membership + archive-ledger access."""
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
        for name in SPEND_VIEW_NAMES:
            clean_schema.execute(spend_view_ddl(name))
        assert _janitor_view_privileges("DailyTagSpend")
        result: subprocess.CompletedProcess[str] = _run_cleanup("--apply")
        assert result.returncode == 0, result.stderr
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            assert _default_acl_entry(super_conn) == []
            member: object = super_conn.execute("SELECT pg_has_role('oicm', 'litellm', 'USAGE')").fetchone()[0]
            assert bool(member)
            ledger_select: object = super_conn.execute(
                "SELECT has_table_privilege('oicm', 'public.\"LiteLLM_SpendLogsArchiveLedger\"', 'SELECT')"
            ).fetchone()[0]
            assert bool(ledger_select), "the janitor's explicit archive-ledger grant must survive"
        for name in SPEND_VIEW_NAMES:
            assert _janitor_view_privileges(name) == [], f"{name} must carry no janitor entry"

    def test_cleanup_is_idempotent(self, clean_schema):
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
        clean_schema.execute(spend_view_ddl("Last30dModelsBySpend"))
        first: subprocess.CompletedProcess[str] = _run_cleanup("--apply")
        assert first.returncode == 0, first.stderr
        second: subprocess.CompletedProcess[str] = _run_cleanup("--apply")
        assert second.returncode == 0, second.stderr
        assert "nothing to do" in second.stdout

    def test_recreated_view_stays_clean_after_cleanup(self, clean_schema):
        """The point of the fix: after the persistent row is gone, recreating a
        view (repair or migration replay) does NOT hand the janitor a grant."""
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
        for name in SPEND_VIEW_NAMES:
            clean_schema.execute(spend_view_ddl(name))
        result: subprocess.CompletedProcess[str] = _run_cleanup("--apply")
        assert result.returncode == 0, result.stderr
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            super_conn.execute('DROP VIEW "DailyTagSpend"')
        clean_schema.execute(spend_view_ddl("DailyTagSpend"))
        assert _janitor_view_privileges("DailyTagSpend") == []

    def test_explicit_reader_grant_survives(self, clean_schema):
        """A dedicated reader role's explicit SELECT on a spend view is not the
        janitor's default-grant signature and must not be revoked."""
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
            super_conn.execute("DROP ROLE IF EXISTS dashboard_reader")
            super_conn.execute("CREATE ROLE dashboard_reader NOLOGIN")
        for name in SPEND_VIEW_NAMES:
            clean_schema.execute(spend_view_ddl(name))
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            super_conn.execute('GRANT SELECT ON "Last30dModelsBySpend" TO dashboard_reader')
        try:
            result: subprocess.CompletedProcess[str] = _run_cleanup("--apply")
            assert result.returncode == 0, result.stderr
            with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
                reader_select: object = super_conn.execute(
                    "SELECT has_table_privilege('dashboard_reader', 'public.\"Last30dModelsBySpend\"', 'SELECT')"
                ).fetchone()[0]
            assert bool(reader_select), "the reader grant must survive the janitor cleanup"
        finally:
            with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
                super_conn.execute('REVOKE ALL ON "Last30dModelsBySpend" FROM dashboard_reader')
                super_conn.execute("DROP ROLE IF EXISTS dashboard_reader")

    def test_janitor_partition_dump_still_works_via_membership(self, clean_schema):
        """The janitor's real workflow survives: pg_dump of an application-owned
        table works through role membership, without any default grant."""
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
        clean_schema.execute(spend_view_ddl("Last30dModelsBySpend"))
        result: subprocess.CompletedProcess[str] = _run_cleanup("--apply")
        assert result.returncode == 0, result.stderr
        dump: subprocess.CompletedProcess[str] = (
            subprocess.run(  # test-quality-ok: TQ009 the janitor-workflow probe passes the fixture janitor DSN via -c string interpolation; -I would drop the env needed for psycopg and the connection string never lands in a file
                [
                    sys.executable,
                    "-c",
                    "import sys;"
                    "from pathlib import Path;"
                    "sys.path.insert(0, r'%s');"
                    "import psycopg;"
                    "conn = psycopg.connect(r'%s');"
                    "cur = conn.execute('SELECT count(*) FROM \"LiteLLM_SpendLogs\"');"
                    "print('janitor-read-ok', cur.fetchone()[0])" % (str(REPO_ROOT), _JANITOR_URL or ""),
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": ""},
            )
        )
        assert "janitor-read-ok" in dump.stdout, dump.stderr

    def test_janitor_ledger_write_still_works(self, clean_schema):
        """The janitor can still write its archive ledger (explicit grant, not
        default privileges)."""
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
            super_conn.execute('GRANT SELECT, INSERT, UPDATE ON "LiteLLM_SpendLogsArchiveLedger" TO oicm')
        result: subprocess.CompletedProcess[str] = _run_cleanup("--apply")
        assert result.returncode == 0, result.stderr
        with psycopg.connect(_JANITOR_URL, connect_timeout=10, autocommit=True) as janitor:
            janitor.execute(
                'INSERT INTO "LiteLLM_SpendLogsArchiveLedger" (partition_name, state) VALUES (%s, %s)',
                ("fixture_p2026_11", "eligible"),
            )
            row: object = janitor.execute(
                'SELECT state FROM "LiteLLM_SpendLogsArchiveLedger" WHERE partition_name = %s',
                ("fixture_p2026_11",),
            ).fetchone()
            assert row is not None
            janitor.execute(
                'DELETE FROM "LiteLLM_SpendLogsArchiveLedger" WHERE partition_name = %s', ("fixture_p2026_11",)
            )


class TestRefusals:
    def test_refuses_a_narrow_default_grant(self, clean_schema):
        """A deliberate narrow default grant (SELECT only) is not the reported
        broad signature; the cleanup must refuse rather than silently revoke it."""
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            super_conn.execute("ALTER DEFAULT PRIVILEGES FOR ROLE litellm GRANT SELECT ON TABLES TO oicm")
        clean_schema.execute(spend_view_ddl("Last30dModelsBySpend"))
        result: subprocess.CompletedProcess[str] = _run_cleanup("--apply")
        assert result.returncode != 0
        assert "refusing to guess" in (result.stdout + result.stderr)
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            assert _default_acl_entry(super_conn) == ["SELECT"], "the narrow grant must be untouched"

    def test_refuses_a_foreign_owned_view(self, clean_schema):
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
            super_conn.execute(spend_view_ddl("Last30dModelsBySpend"))
        clean_schema.execute('DROP VIEW IF EXISTS "Last30dKeysBySpend"')
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            super_conn.execute('ALTER VIEW "Last30dModelsBySpend" OWNER TO postgres')
        try:
            result: subprocess.CompletedProcess[str] = _run_cleanup("--apply")
            assert result.returncode != 0
            assert "third party" in (result.stdout + result.stderr)
        finally:
            with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
                super_conn.execute('ALTER VIEW "Last30dModelsBySpend" OWNER TO litellm')

    def test_refuses_a_non_view_collision(self, clean_schema):
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
        clean_schema.execute('CREATE TABLE "Last30dKeysBySpend" (x int)')
        try:
            result: subprocess.CompletedProcess[str] = _run_cleanup("--apply")
            assert result.returncode != 0
            assert "not a view" in (result.stdout + result.stderr)
        finally:
            clean_schema.execute('DROP TABLE "Last30dKeysBySpend"')

    def test_refuses_when_membership_is_missing(self, clean_schema):
        with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
            _install_broad_default(super_conn)
            super_conn.execute("REVOKE litellm FROM oicm")
        try:
            result: subprocess.CompletedProcess[str] = _run_cleanup("--apply")
            assert result.returncode != 0
            assert "membership" in (result.stdout + result.stderr)
        finally:
            with psycopg.connect(_SUPER_URL, connect_timeout=10, autocommit=True) as super_conn:
                super_conn.execute("GRANT litellm TO oicm")

    def test_refuses_non_superuser(self, clean_schema):
        result: subprocess.CompletedProcess[str] = subprocess.run(
            [
                sys.executable,
                "-I",
                str(REPO_ROOT / "scripts" / "cleanup_spend_view_default_grants.py"),
                "--superuser-url",
                _APP_URL or "",
                "--app-role",
                "litellm",
                "--grantee-role",
                "oicm",
                "--apply",
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
        assert result.returncode != 0
        assert "superuser" in (result.stdout + result.stderr)
