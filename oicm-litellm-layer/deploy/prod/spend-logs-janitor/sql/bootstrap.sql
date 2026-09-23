-- Canonical DB-side setup for spend-logs archival on dev.
-- Idempotent; every statement is safe to re-run.
--
-- Owns:
--   * LiteLLM_SpendLogsArchiveLedger  (state per partition)
--   * LiteLLM_SpendLogsArchiveMeta    (runtime-discoverable config: master_table)
--   * Ownership of the master table + its partitions + both archiver tables (litellm)
--   * Grants the janitor needs (ledger R/W, CREATEDB for pg_restore verify)
--   * Role attribute litellm needs (CREATEDB)
--
-- Does NOT do: the one-shot partition conversion. For that see
-- db_scripts/partition_spend_logs.sql (upstream LiteLLM runbook).

-- ===========================================================================
-- Config (runtime-discoverable master table)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS "LiteLLM_SpendLogsArchiveMeta" (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO "LiteLLM_SpendLogsArchiveMeta" (key, value)
VALUES ('master_table', 'LiteLLM_SpendLogs')
ON CONFLICT (key) DO NOTHING;

-- ===========================================================================
-- Ledger table
-- ===========================================================================

CREATE TABLE IF NOT EXISTS "LiteLLM_SpendLogsArchiveLedger" (
    partition_name        text PRIMARY KEY,
    bound_lower           timestamptz NOT NULL,
    bound_upper           timestamptz NOT NULL,
    state                 text NOT NULL,
    artifact_uri          text,
    artifact_sha256       text,
    artifact_size_bytes   bigint,
    rows_at_dump          bigint,
    transitioned_at       timestamptz NOT NULL DEFAULT now(),
    last_error            text
);

-- Valid states the janitor's state machine actually uses.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = '"LiteLLM_SpendLogsArchiveLedger"'::regclass
          AND conname  = 'valid_state'
    ) THEN
        ALTER TABLE "LiteLLM_SpendLogsArchiveLedger" DROP CONSTRAINT valid_state;
    END IF;
    ALTER TABLE "LiteLLM_SpendLogsArchiveLedger"
        ADD CONSTRAINT valid_state
        CHECK (state IN ('eligible','detached','dumped','verified','dropped','restored'));
END $$;

CREATE INDEX IF NOT EXISTS "LiteLLM_SpendLogsArchiveLedger_state_idx"
    ON "LiteLLM_SpendLogsArchiveLedger" (state);

-- ===========================================================================
-- Master table guard: refuse to run if the master table isn't partitioned
-- ===========================================================================

DO $$
DECLARE
    master text;
BEGIN
    SELECT value INTO master
    FROM "LiteLLM_SpendLogsArchiveMeta"
    WHERE key = 'master_table';

    IF NOT EXISTS (
        SELECT 1
        FROM pg_class
        WHERE relname = master
          AND relkind = 'p'
    ) THEN
        RAISE EXCEPTION 'Master table % is not partitioned (relkind != p). Run db_scripts/partition_spend_logs.sql first.', master;
    END IF;
END $$;

-- ===========================================================================
-- Ownership and privileges for the janitor service account
-- ===========================================================================

ALTER USER litellm WITH CREATEDB;

DO $$
DECLARE
    master text;
    r      record;
BEGIN
    SELECT value INTO master
    FROM "LiteLLM_SpendLogsArchiveMeta"
    WHERE key = 'master_table';

    EXECUTE format('ALTER TABLE %I OWNER TO litellm', master);

    FOR r IN
        SELECT c.relname
        FROM pg_partition_tree(format('public.%I', master)::regclass) t
        JOIN pg_class c ON c.oid = t.relid
        WHERE c.relkind IN ('r','p')
        UNION
        SELECT master || '_pdefault'
    LOOP
        EXECUTE format('ALTER TABLE %I OWNER TO litellm', r.relname);
    END LOOP;
END $$;

ALTER TABLE "LiteLLM_SpendLogsArchiveLedger" OWNER TO litellm;
ALTER TABLE "LiteLLM_SpendLogsArchiveMeta"   OWNER TO litellm;

GRANT SELECT, INSERT, UPDATE ON "LiteLLM_SpendLogsArchiveLedger" TO litellm;
GRANT SELECT, INSERT, UPDATE ON "LiteLLM_SpendLogsArchiveMeta"   TO litellm;
GRANT CREATE ON DATABASE litellm TO litellm;

-- Prod janitor connects as oicm. Grant it ledger access and litellm role
-- membership so it can DETACH partitions owned by litellm.
ALTER TABLE "LiteLLM_SpendLogsArchiveLedger" OWNER TO oicm;
ALTER TABLE "LiteLLM_SpendLogsArchiveMeta"   OWNER TO oicm;
GRANT SELECT, INSERT, UPDATE ON "LiteLLM_SpendLogsArchiveLedger" TO oicm;
GRANT SELECT, INSERT, UPDATE ON "LiteLLM_SpendLogsArchiveMeta"   TO oicm;
GRANT CREATE ON DATABASE litellm TO oicm;
ALTER USER oicm WITH CREATEDB;
GRANT litellm TO oicm;
ALTER DEFAULT PRIVILEGES FOR ROLE oicm GRANT ALL ON TABLES TO oicm;

-- ===========================================================================
-- Default privileges for future partitions LiteLLM creates
-- ===========================================================================

ALTER DEFAULT PRIVILEGES FOR ROLE litellm GRANT ALL ON TABLES TO litellm;

-- ===========================================================================
-- Legacy table cleanup (opt-in, one-shot)
-- ===========================================================================

-- Set BOOTSTRAP_DROP_LEGACY=true to drop the pre-partitioning heap table.
-- 12 GiB at time of writing; drop only after a stable soak period.
DO $$
BEGIN
    IF current_setting('app.bootstrap_drop_legacy', true) = 'true' THEN
        EXECUTE 'DROP TABLE IF EXISTS "LiteLLM_SpendLogs_legacy"';
        RAISE NOTICE 'Dropped LiteLLM_SpendLogs_legacy';
    ELSE
        RAISE NOTICE 'BOOTSTRAP_DROP_LEGACY not set; keeping LiteLLM_SpendLogs_legacy';
    END IF;
END $$;
