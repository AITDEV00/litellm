# Model Performance Tab — Optimization Path Logic Map

> **Technique**: [Logic Mapping](../techniques/logic_mapping_technique.md) —
> trace-through-before-you-build. This document maps the **entire** data flow
> that makes the Model Performance tab load faster, from request ingestion to
> the dashboard response, with exact file:line references.

## TL;DR — The One-Sentence Answer

The Model Performance tab used to run a full `GROUP BY` + window sort over the
raw `LiteLLM_SpendLogs` table (millions of rows) on every page load. Now, each
request **pre-aggregates** its metrics into a tiny per-minute rollup table at
write time, so the page load reads a few hundred rollup rows instead of a
multi-GB table scan. The two expensive metrics that don't survive normal
`GROUP BY` aggregation — **peak concurrency** and **p50/p95 TTFT** — are handled
with two special structures: `(starts, ends)` counters (peak recomputed at read)
and a fixed 32-bin log histogram (percentiles reconstructed at read).

---

## Top-Level Flow Diagram

```
 REQUEST INGRESS                                    REQUEST SERVING (page load)
 ──────────────                                     ──────────────────────────
 POST /chat/completions                             GET /model/performance
      │                                                     │
      v                                                     v
 [Proxy] ──► litellm.completion()                  [model_performance] route
      │        (business logic)                            │
      │                                                     │
      v                                                     v
 [SpendLogsPayload]                              Entity scoped? (team/user/key...)
      │                                          ┌─────yes──────┴──────no──────────┐
      v                                          ▼                              ▼
 [DBSpendUpdateWriter]                    [RAW SpendLogs scan]            [Fast path decision]
   ._batch_database_updates()              (entity columns filter)        live window 1m-1h?
      │                                                                 ┌──no─────────────yes──┐
      ▼                                                                    ▼                        ▼
 [add_..._to_model_performance_rollup()]                            [ROLLUP read]          [Prometheus read]
   builds 1-min transaction                                    (fast path, GLOBAL only)
      │                                                                    │
      v                                                                     │
 [in-memory ModelPerformanceRollupUpdateQueue]                              │
   (merge monoid)                                                           │
      │                                                                     │
      v  (periodic flush)                                                   │
 [Redis buffer] ────► [drain scheduler job] ──► [Postgres                  │
   (cross-instance)     every 2.3x interval      upsert into               │
                       picks up the buffer     LiteLLM_ModelPerformanceRollup]
                                                          ▲                 │
                                                          │  1-minute buckets, indexed by bucket_start
                                                          └─────────────────┘
```

---

## WRITE PATH — Per-Request Pre-Aggregation (what makes reads fast)

Every request contributes one small transaction; nothing heavy happens per request
(in-memory only, zero DB write overhead in the hot path).

### Step W1 — Entry point: spend-log batching

`litellm/proxy/db/db_spend_update_writer.py:435` — `DBSpendUpdateWriter._batch_database_updates()`
is called for every completed request (from the proxy's spend-log pipeline). It fans
out the `SpendLogsPayload` to several updaters (user/team/org/tag spend, and now the
model performance rollup).

### Step W2 — Build the per-request rollup transaction

```
litellm/proxy/db/db_spend_update_writer.py:2300
DBSpendUpdateWriter.add_spend_log_transaction_to_model_performance_rollup(payload)
```

- Skips the request if `cache_hit == "True"` or `model_group` is empty (matches the
  read path's cache-hit exclusion).
- Computes the **1-minute bucket**: `bucket_start = startTime.replace(second=0, microsecond=0)`.
- Computes **throughput** = `completion_tokens / request_duration_ms * 1000`.
- Computes **TTFT** = `completionStartTime - startTime` (seconds), measured against
  the **exact** start time, not the truncated bucket, and **skipped entirely** when
  `completionStartTime == endTime` (matches the raw read path's degenerate-row
  exclusion so p50/p95 agree).
- Records **concurrency**: the start bucket gets `starts = 1`. If the request ends in
  the same minute, that bucket also gets `ends = 1`; if it ends in a **later** minute,
  the start bucket keeps `ends = 0` and a separate `request_count = 0` transaction is
  emitted for the **end bucket** with `ends = 1`. The `-1` therefore lands in the
  minute the request actually ends, so the running-sum peak reflects how long the
  request was active instead of cancelling it out in its own start minute.
- Builds a `ModelPerformanceRollupTransaction` and calls
  `model_performance_rollup_update_queue.add_update(...)`.

### Step W3 — In-memory monoid queue

```
litellm/proxy/db/db_transaction_queue/model_performance_rollup_update_queue.py
ModelPerformanceRollupUpdateQueue
```

- `add_update(update)` puts the transaction on an `asyncio.Queue`.
- `get_aggregated_rollup_transactions()` merges same-bucket transactions into one via
  the **merge monoid** (`merge_transactions`): scalar sums add, `min`/`max` combine,
  histogram counts add element-wise, `starts`/`ends` add.
- The monoid makes the buffer size bounded even under high throughput.

### Step W4 — Flush to Redis

```
litellm/proxy/db/db_transaction_queue/redis_update_buffer.py:698
store_in_memory_model_performance_rollup_updates_in_redis(queue)
```

Flushes the aggregated in-memory dict to a Redis list under key
`litellm_model_performance_rollup_update_buffer` so multiple proxy instances/workers
share the pending updates.

### Step W5 — Drain job batch-upserts to Postgres

```
litellm/proxy/utils.py:5543   update_model_performance_rollup(prisma_client, ...)
litellm/proxy/proxy_server.py:8052  (scheduler registration)
```

- Registered on APScheduler at
  `batch_writing_interval * MODEL_PERFORMANCE_ROLLUP_BATCH_MULTIPLIER` (2.3x the main
  job interval) — a longer interval keeps upsert contention low.
- Job does a Redis-first drain (same pattern as daily tag spend):
  - `_commit_model_performance_rollup_to_db_with_redis()` if Redis is on — pops from
    the Redis buffer, then `DBSpendUpdateWriter.update_model_performance_rollup()`.
  - `_commit_model_performance_rollup_to_db()` otherwise — flushes the in-memory
    queue directly.
- `update_model_performance_rollup()` (line 1983) batches transactions (size 200) and
  calls `_execute_rollup_upsert()`.

### Step W6 — The upsert SQL (merge-on-conflict)

```
litellm/proxy/db/db_spend_update_writer.py:107  _execute_rollup_upsert(...)
```

A single multi-row `INSERT ... ON CONFLICT ("model_group","bucket_start") DO UPDATE`
which re-applies the monoid in SQL:
- sums add, `LEAST`/`GREATEST` combine min/max,
- `_rollup_array_add_bigint(hist, hist)` element-wise adds histogram counts,
- `starts`/`ends` add.

This whole write path is **zero per-request DB cost** — the request only appends to an
in-memory queue.

---

## READ PATH — Fast Global View (what the user sees)

### Step R1 — Entry point: the route

```
litellm/proxy/model_metrics_endpoints/model_performance_endpoints.py:218
model_performance(window, model_group, team_id, ..., step, start_time, end_time)
```

Builds an `EntityScope` from the query filters and decides which path to take.

### Step R2 — Path selection

```
line 287  if scope.is_empty and not has_custom_range and window in _PROMETHEUS_WINDOWS:
              → Prometheus (live 1m-1h)
line 496  if scope.is_empty:
              → ROLLUP (fast path, GLOBAL view, 24h/7d/custom)
line 498  else:
              → RAW SpendLogs scan (entity-scoped)
```

| Condition | Path | Why |
|---|---|---|
| entity filter present (team/user/key...) | **RAW SpendLogs** | must filter by entity columns the rollup/Prometheus don't carry |
| custom time range | **RAW SpendLogs** | Prometheus short-window only; rollup won't cover arbitrary ranges |
| global + window `1m`-`1h` | **Prometheus** | already fast, live data, 10s poll |
| global + window `24h`/`7d` | **ROLLUP** | the old slow path, now fast |

### Step R3 — ROLLUP read (the optimization)

```
line 714  _fetch_db_performance_from_rollup(window, model_group, bucket_interval, start, end)
```

1. Cache check (`_DB_CACHE_TTL`: 24h=120s, 7d=300s).
2. Compute the range and effective bucket interval (for custom ranges, scale the bucket
   to ~288 points).
3. SQL `SELECT ... FROM LiteLLM_ModelPerformanceRollup WHERE bucket_start >= $1 AND
   bucket_start < $2 AND (model_group = $3 OR $3 IS NULL)` — reads **only the rollup
   slice**, not the raw log.
4. For ranges ≥ 14 days, uses a dedicated long-timeout Prisma client
   (`_get_heavy_query_prisma_client()`).
5. Groups rows by `model_group`, then calls `_rollup_minutes_to_model()` per model.

### Step R4 — 1-minute → effective bucket aggregation + peak + percentile

```
line 821  _rollup_minutes_to_model(model_group, minutes, start, end, bucket_interval)
```

- Sorts the 1-minute rows chronologically.
- Aligns each to the coarser bucket (5-min, 1h, 6h, ...).
- Sums counts/tokens/throughput/TTFT across sub-buckets; combines min/max.
- **Recomputes the peak concurrency** via a **continuous** running-sum of
  `(starts, ends)` over the **whole window**:
  - `running += starts - ends` per minute; `peak = max(peak, running)`.
  - The running sum is **NOT reset at coarse-bucket boundaries** — a request that
    spans a boundary must keep its concurrency — and within a minute it applies the
    **net** (starts - ends), since the sub-minute ordering is lost. Applying all
    starts then all ends within a minute would over-count the peak.
- **Folds each bucket's histogram** into a whole-window histogram (element-wise
  count add).
- After `_summarize_time_series()` fills avg fields, sets `summary["p50_ttft"]` /
  `summary["p95_ttft"]` / `summary["p99_ttft"]` from `histogram_percentile(0.5/0.95/0.99,
  edges, whole-window_counts)` so the summary percentiles reflect the true per-request
  distribution, not the bucket means.
- Emits the time series (throughput, concurrent, TTFT-mean) per bucket.

### Step R5 — Cache

The rollup result is cached in `_DB_CACHE` (InMemoryCache) for 120s (24h) / 300s (7d),
so repeated loads within the TTL are served from memory.

---

## The Two "Hard" Metrics — how they survive aggregation

### Concurrency (peak)

Naive: `MAX(COUNT(*))` per bucket is wrong. A request spans buckets; the peak is the
max number of simultaneously-active requests inside a bucket. The raw path does a
`running SUM(change) OVER`, needing the whole range. The rollup stores `starts`/`ends`
per 1-minute bucket (`starts` in the request's start minute, `ends` in its end minute),
and the read recomputes the running-sum peak over the (tiny) minute sequence. No extra
scan.

```
+1 at start  →  starts counter for the START minute bucket
-1 at end    →  ends counter for the END minute bucket
read:  running = 0
       for each minute: running += starts - ends; peak = max(peak, running)
```

Two properties make this a good approximation of the raw second-resolution peak:
the running sum is continuous across coarse-bucket boundaries (a request straddling a
boundary keeps its concurrency), and within a minute only the net `starts - ends` is
applied because sub-minute ordering is lost. A request whose start and end share a
minute keeps both counters in that minute, netting to no change there. The remaining
error vs. the raw second-resolution value is the inherent 1-minute quantization
(e.g. a true peak of 34 reads back as 33), which is acceptable for monitoring. The
exact `(net, peak)` monoid from the design docs would need finer-grained buckets and
a schema change, so it is deliberately out of scope.

### TTFT percentiles (p50/p95/p99)

`PERCENTILE_CONT` is not mergeable across sub-intervals. The rollup instead stores a
fixed **32-bin log-spaced histogram** of TTFT. Histograms **are** mergeable
(element-wise count add, both in the DB upsert and in-memory). The read reconstructs
a percentile by **linear interpolation in log10 space** within the crossing bin
(`histogram_percentile`), tracking the true distribution far better than an arithmetic
mid-bin estimate. Because the tail bins are log-spaced (~1.3-2x wide), arithmetic
midpoints pinned p95 to the bin centre and over-stated long latencies by up to ~2x;
log10 interpolation brings p95 to within a few percent of the raw `PERCENTILE_CONT`
in the tail (e.g. DeepSeek p95 47.8 vs raw 45.8). p99 is surfaced the same way.

---

## Data Contract (TypedDict)

```
litellm/proxy/_types.py:4582  ModelPerformanceRollupTransaction
```

| field | meaning |
|---|---|
| `model_group` | client-requested model name |
| `bucket_start` | 1-minute-aligned ISO timestamp |
| `request_count` | # requests in bucket |
| `completion_tokens` | summed |
| `throughput_tokens_sum` | summed, for avg |
| `ttft_seconds_sum` / `ttft_seconds_sum_sq` | for mean/variance |
| `ttft_seconds_min` / `ttft_seconds_max` | combined min/max |
| `ttft_histogram_edges` / `ttft_histogram_counts` | log-bucketed TTFT histogram |
| `starts` / `ends` | concurrency +1/-1 counters |

---

## Table / Index / Config Constants

- Table: `LiteLLM_ModelPerformanceRollup`, PK `("model_group","bucket_start")`,
  index on `bucket_start`.
  (`litellm-proxy-extras/.../migrations/20260807120000_add_model_performance_rollup/migration.sql`)
- Redis key: `REDIS_MODEL_PERFORMANCE_ROLLUP_UPDATE_BUFFER_KEY` = `litellm_model_performance_rollup_update_buffer`
  (`litellm/constants.py:263`)
- Job: `DB_MODEL_PERFORMANCE_ROLLUP_UPDATE_JOB_NAME` = `db_model_performance_rollup_update_job`
  (`litellm/constants.py:1449`)
- Interval multiplier: `MODEL_PERFORMANCE_ROLLUP_BATCH_MULTIPLIER = 2.3` (`litellm/constants.py:1506`)

---

## What Remains Slow (out of scope)

- **Entity-scoped** views (per-team/user/API-key) and **custom time ranges** still
  read the raw `LiteLLM_SpendLogs`, because they must filter on entity columns.
  Native time partitioning (already shipped in `db_scripts/partition_spend_logs.sql`)
  is the recommended next step for those.

---

## Files & Line Map

| File | Lines | Role |
|---|---|---|
| `litellm/proxy/db/db_spend_update_writer.py` | 107 (upsert SQL), 2300 (enqueue), 1983 (batch upsert) | write path |
| `litellm/proxy/db/db_transaction_queue/model_performance_rollup_update_queue.py` | whole file | in-memory monoid queue + histogram helpers |
| `litellm/proxy/db/db_transaction_queue/redis_update_buffer.py` | 698-733 | Redis buffer |
| `litellm/proxy/utils.py` | 5543 | drain job |
| `litellm/proxy/proxy_server.py` | 8052 | scheduler registration |
| `litellm/proxy/model_metrics_endpoints/model_performance_endpoints.py` | 218 (route), 287/496 (routing), 714 (rollup read), 821 (bucket + histogram) | read path |
| `litellm/proxy/_types.py` | 4582 | `ModelPerformanceRollupTransaction` |
| `litellm/constants.py` | 263/1449/1506 | config |