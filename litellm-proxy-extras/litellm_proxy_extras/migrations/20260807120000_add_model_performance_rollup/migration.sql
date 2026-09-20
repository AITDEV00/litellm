-- CreateTable
-- One-minute rollup of LiteLLM_SpendLogs for the /model/performance endpoint's
-- global (no entity-filter) view. Reads go straight from this table instead of
-- scanning the (multi-GB) raw spend log over large windows.
--
-- The rollup is keyed only by (model_group, bucket_start). Entity-scoped
-- requests (team/user/api_key/... filters) still read the raw spend log.
--
-- Concurrency is stored as (starts, ends) counters per bucket: the read path
-- recomputes the exact running-sum peak from these, rather than storing a
-- write-time approximation.
--
-- ttft_seconds_histogram_counts is a fixed-width log-bucketed array of counts
-- (edges in ttft_seconds_histogram_edges), so p50/p95 can be reconstructed
-- exactly by walking cumulative counts.
CREATE TABLE IF NOT EXISTS "LiteLLM_ModelPerformanceRollup" (
    "model_group" TEXT NOT NULL,
    "bucket_start" TIMESTAMPTZ NOT NULL,
    "request_count" BIGINT NOT NULL DEFAULT 0,
    "completion_tokens" BIGINT NOT NULL DEFAULT 0,
    "throughput_tokens_sum" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "ttft_seconds_sum" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "ttft_seconds_sum_sq" DOUBLE PRECISION NOT NULL DEFAULT 0,
    "ttft_seconds_min" DOUBLE PRECISION,
    "ttft_seconds_max" DOUBLE PRECISION,
    "ttft_histogram_edges" DOUBLE PRECISION[] NOT NULL DEFAULT '{}',
    "ttft_histogram_counts" BIGINT[] NOT NULL DEFAULT '{}',
    "starts" BIGINT NOT NULL DEFAULT 0,
    "ends" BIGINT NOT NULL DEFAULT 0,

    CONSTRAINT "LiteLLM_ModelPerformanceRollup_pkey" PRIMARY KEY ("model_group", "bucket_start")
);

-- Index to let the read path scan by time window efficiently (both the rollup
-- rows and the entity-scoped raw-scan fallback).
CREATE INDEX IF NOT EXISTS "LiteLLM_ModelPerformanceRollup_bucket_start_idx"
ON "LiteLLM_ModelPerformanceRollup" ("bucket_start");

-- Element-wise add of two BIGINT[] arrays, used by the rollup upsert's
-- ON CONFLICT merge to combine histogram counts. Shorter arrays are padded
-- with zeros to the longer array's length.
CREATE OR REPLACE FUNCTION _rollup_array_add_bigint(a bigint[], b bigint[])
RETURNS bigint[]
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    n int;
    result bigint[];
BEGIN
    n := GREATEST(array_length(a, 1), array_length(b, 1));
    IF n IS NULL THEN
        RETURN '{}'::bigint[];
    END IF;
    result := ARRAY(
        SELECT COALESCE(a[i], 0::bigint) + COALESCE(b[i], 0::bigint)
        FROM generate_series(1, n) AS i
    );
    RETURN result;
END;
$$;