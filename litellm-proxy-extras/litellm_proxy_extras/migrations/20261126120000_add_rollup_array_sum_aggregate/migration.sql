-- Element-wise bigint[] sum aggregate for the model-performance rollup read.
--
-- The /model/performance rollup read folds a window's 1-minute histogram rows
-- into coarse buckets. Collecting them with json_agg made each result row
-- carry one count array per minute in the bucket, so a full-range request
-- shipped a ~62MB JSON payload; the prisma query engine's serde
-- deserialization of that payload ballooned a fresh engine to 4.5GB RSS
-- (engine resident memory is a high-water mark, never returned to the OS),
-- which OOMKilled prod pods. Summing element-wise in SQL bounds the payload
-- to one fixed-size count array per row.
--
-- Built on _rollup_array_add_bigint (20260807120000_add_model_performance_rollup),
-- the same element-wise add the write path's ON CONFLICT merge uses.
--
-- DDL only: no row rewrites (see tests/code_coverage_tests/check_migrations_no_data_rewrites.py).

CREATE OR REPLACE AGGREGATE _rollup_array_sum(bigint[]) (
    SFUNC = _rollup_array_add_bigint,
    STYPE = bigint[],
    INITCOND = '{}'
);
