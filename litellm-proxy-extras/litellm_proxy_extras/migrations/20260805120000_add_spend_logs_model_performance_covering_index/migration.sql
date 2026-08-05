-- CreateIndex
-- Covering index for the Model Performance (per-model-group) DB query on
-- LiteLLM_SpendLogs. The query scans by startTime (filtering out cache hits)
-- and aggregates completion_tokens / request_duration_ms / completionStartTime
-- / endTime per model_group. Without a covering index the aggregation falls
-- back to a sequential scan of the whole (multi-GB) table over large custom
-- ranges (e.g. year-to-date), which made the /model/performance endpoint take
-- ~55s. INCLUDE columns let the planner satisfy the aggregation from the index
-- alone (index-only scan), cutting the YTD query to well under the Prisma
-- HTTP timeout. Prisma's @@index cannot express covering INCLUDE columns, so
-- this is shipped as a hand-written migration.
CREATE INDEX IF NOT EXISTS "LiteLLM_SpendLogs_model_performance_covering_idx"
ON "LiteLLM_SpendLogs" ("startTime")
INCLUDE ("cache_hit", "model_group", "endTime", "completion_tokens", "request_duration_ms", "completionStartTime");