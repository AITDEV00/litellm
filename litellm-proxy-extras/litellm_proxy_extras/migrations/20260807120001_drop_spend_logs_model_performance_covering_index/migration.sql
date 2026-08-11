-- DropIndex
-- The model performance page now reads the 1-minute rollup table
-- (LiteLLM_ModelPerformanceRollup) for the global view, so the covering index
-- on LiteLLM_SpendLogs is no longer needed. Dropping it removes the write
-- amplification of maintaining a large INCLUDE index on every spend-log insert.
DROP INDEX IF EXISTS "LiteLLM_SpendLogs_model_performance_covering_idx";