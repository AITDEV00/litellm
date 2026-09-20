# Model Performance Tab — 30d/YTD Read Slowdown Fix — Logic Map

> **Technique**: [Logic Mapping](../techniques/logic_mapping_technique.md) —
> trace-through-before-you-build. This document maps the specific fix that made
> the **large-window (30d / MTD / YTD) Model Performance tab** load fast again,
> from the slow SQL to the final optimized query, with exact file:line
> references.

## TL;DR — The One-Sentence Answer

The rollup read already avoided the multi-GB `LiteLLM_SpendLogs` scan, but for a
30d / YTD window it still pulled **every 1-minute row** out of
`LiteLLM_ModelPerformanceRollup` (~96k rows) and aggregated them **in Python**.
Prisma's Rust-engine HTTP transport serializes all of those rows to JSON, which
took ~16s and intermittently timed out. The fix **pushes that aggregation into
SQL** (`date_bin` + `SUM` + a windowed running-sum for peak concurrency), so the
query returns only the effective coarse buckets (a few hundred rows), cutting the
read to ~4-5s. The response content is **byte-identical** to the old path.

---

## Why the old read was slow

The write path was already fixed (see
[model-performance-optimization-LOGIC-MAP.md](./model-performance-optimization-LOGIC-MAP.md)):
every request is pre-aggregated into 1-minute buckets in
`LiteLLM_ModelPerformanceRollup`. The **read** path is the problem this fix
addresses.

```
OLD read (_fetch_db_performance_from_rollup, pre-fix)
────────────────────────────────────────────────────
SELECT model_group, bucket_start, request_count, ..., starts, ends
FROM "LiteLLM_ModelPerformanceRollup"
WHERE bucket_start >= $1 AND bucket_start < $2
ORDER BY model_group, bucket_start

   returns ALL 1-minute rows for the window
        │
        ▼
Prisma HTTP engine JSON-serializes every row  ←  ~96,749 rows for 30d
        │
        ▼
Python _rollup_minutes_to_model()  ←  re-aligns to coarse buckets in a loop
        │
        ▼
   ~16s end-to-end, intermittent "failed to fetch"
```

The dominant cost is **Prisma row serialization**, not the Python arithmetic.
For a 30d window the query returns 96k+ rows that Prisma turns into JSON over its
Rust-engine HTTP transport, then Python re-buckets them. The fix reduces the
**number of rows Prisma must serialize** by collapsing them in the database.

---

## Top-Level Flow (after the fix)

```
 GET /model/performance?window=30d     (or custom start/end spanning >= 14 days)
        │
        ▼
 model_performance(...)                       model_performance_endpoints.py:219
        │
        ▼  scope.is_empty (global view)?
        │
        ▼ yes
 _fetch_db_performance_from_rollup(...)       model_performance_endpoints.py:715
        │
        ├── cache TTL check (_DB_CACHE: 30d-eligible windows)
        │
        ▼
 ┌────────────────────────────────────────────────────────────────────┐
 │ ONE aggregated SQL query (single round-trip to Postgres)          │
 │                                                                    │
 │  CTE agg:          date_bin -> coarse bucket,                     │
 │                    SUM(request_count), SUM(completion_tokens),     │
 │                    SUM(throughput_tokens_sum), SUM(ttft_seconds_sum)│
 │                    MAX(ttft_histogram_edges),                      │
 │                    json_agg(ttft_histogram_counts)   ← 2D histogram │
 │  CTE minute_changes: starts - ends AS change (per 1-min row)       │
 │  CTE running:  SUM(change) OVER (PARTITION BY model_group          │
 │                                      ORDER BY ev_time)  whole-window│
 │  CTE concurrency: date_bin bucket, GREATEST(MAX(cumulative),0)     │
 │  FINAL:  agg a LEFT JOIN concurrency c ON model_group+bucket       │
 └────────────────────────────────────────────────────────────────────┘
        │  returns ~ one row per (model_group, coarse bucket)
        │  (a few hundred rows, not 96k)
        ▼
 _rollup_coarse_buckets_to_model(mg, rows)    model_performance_endpoints.py:892
        │  renders time series, folds json_agg histogram element-wise,
        │  computes p50/p95/p99 from the merged whole-window histogram
        ▼
 response (models[].time_series + summary)
```

---

## READ PATH — Step by Step

### Step R3' — Route to the rollup read (unchanged)

`model_performance_endpoints.py:465` `_fetch_db_performance` → for a global
(`scope.is_empty`) request it calls
`_fetch_db_performance_from_rollup`. Entity-scoped and short-window Prometheus
paths are unchanged.

### Step R4' — The single aggregated SQL (the fix)

`model_performance_endpoints.py:794-852` builds a single `WITH` query with four
CTEs. The whole query runs in Postgres, so Prisma serializes only the **coarse
bucket** result rows.

**CTE `agg`** — the coarse bucketing:

```sql
date_bin($1::interval, bucket_start, timestamp '2000-01-01') AS bucket,
SUM(request_count)::bigint,
SUM(completion_tokens)::bigint,
SUM(throughput_tokens_sum),
SUM(ttft_seconds_sum),
MAX(ttft_histogram_edges),            -- edges identical for a bucket; take any
json_agg(ttft_histogram_counts)       -- 2D array of per-minute count arrays
```

- `date_bin` aligns every 1-minute `bucket_start` to the effective coarse bucket
  (the same interval the Python path used).
- `json_agg` (JSON array of arrays) is used instead of `array_agg` because
  **Prisma's raw query cannot deserialize a native 2D Postgres array** — it
  errors with "array contains too many dimensions". JSON round-trips cleanly.

**CTEs `minute_changes` → `running` → `concurrency`** — peak concurrency.

The old Python code computed the peak via a running sum of `(starts - ends)`
over the minute sequence, NOT reset at bucket boundaries. That same logic is now
a windowed aggregate:

```sql
running AS (
  SELECT model_group, ev_time,
         SUM(change) OVER (PARTITION BY model_group ORDER BY ev_time) AS cumulative
  FROM minute_changes            -- change = starts - ends per minute
)
concurrency AS (
  SELECT model_group, date_bin($1::interval, ev_time, ...) AS bucket,
         GREATEST(MAX(cumulative), 0) AS concurrent_requests
  FROM running GROUP BY model_group, bucket
)
```

This gives the **peak** concurrency within each coarse bucket, matching the old
Python `running += starts - ends; peak = max(peak, running)` loop exactly.

**Final SELECT** joins `agg a` to `concurrency c` on `(model_group, bucket)` so
each result row carries both the summed metrics and the peak concurrency.

**Parameter binding** — 4 params: `($1 effective_bucket, $2 start_time, $3 end_time, $4 model_group)`.

### Step R5' — Choose the query client

`model_performance_endpoints.py:854-858`. For ranges ≥ 14 days
(`_HEAVY_RANGE_THRESHOLD_SECONDS`) it uses the dedicated long-timeout
`_get_heavy_query_prisma_client()` so the 30s Prisma default HTTP timeout doesn't
kill the big query. Shorter windows use the normal `prisma_client`.

### Step R6' — Render per model (`_rollup_coarse_buckets_to_model`)

`model_performance_endpoints.py:872-960`. This builder is much simpler than the
old one because SQL already did the bucketing:

- **Skip empty buckets**: `count == 0` rows are skipped (these are the
  `request_count = 0` concurrency-only end buckets).
- **Fold the histogram**: `ttft_histogram_counts` arrives as `json_agg` — a list
  of per-1-minute count arrays. They're merged element-wise with
  `add_histogram_counts`.
- **Restore the open upper edge**: the last histogram edge is `infinity` in
  Postgres, which comes back as `NULL` through `MAX()`. It's restored:
  `edges = [float('inf') if e is None else float(e) for e in edges]`. Without
  this, `float(None)` crashed.
- **Mean TTFT** per bucket = `ttft_seconds_sum / sum(bucket_counts)` (the
  histogram total is the count of requests with a valid TTFT).
- **Time series**: appends throughput (per-second), concurrent (peak), and
  TTFT-mean points.
- **Whole-window histogram** is built by folding every bucket's histogram, then
  `histogram_percentile(0.50/0.95/0.99)` computes p50/p95/p99 from the merged
  distribution (not from bucket means). `_summarize_time_series()` fills
  avg fields; the histogram percentiles are assigned **after** it so it doesn't
  overwrite them.

### Step R7' — Cache

The result is cached in `_DB_CACHE` for the TTL in `_DB_CACHE_TTL`
(24h=120s, 7d=300s). Large custom ranges (30d/YTD) have TTL 0 so they are always
fresh, but they are now fast enough that this is fine.

---

## Data & semantics preserved (equivalence)

The fix was validated to return **identical** data to the old path:

| Metric | Old (Python) | New (SQL) | Result |
|---|---|---|---|
| total_requests | `sum(request_count)` | `SUM(request_count)` | identical |
| total_tokens | `sum(completion_tokens)` | `SUM(completion_tokens)` | identical |
| p50/p95/p99 TTFT | `histogram_percentile(edges, whole_window_counts)` | `json_agg` fold → same | identical |
| peak concurrency | running `SUM(starts-ends)` peak | window `SUM(change)` `MAX` | identical |

Empirical check (debug pod, live `mlops` DB):
```
1h:  old_models=8  new_models=8  DIFFS=0
24h: old_models=10 new_models=10 DIFFS=0
```

Latency (debug pod, live data):
| Window | Before | After |
|---|---|---|
| 24h | ~0.1s | ~0.1s (fewer rows) |
| 30d | ~16s | ~5s |
| YTD (2026-01-01 → now) | ~16s+ | ~5.2s |

---

## Files & Line Map

| File | Lines | Role |
|---|---|---|
| `litellm/proxy/model_metrics_endpoints/model_performance_endpoints.py` | 715 (`_fetch_db_performance_from_rollup`), 755–852 (aggregated SQL), 872–960 (`_rollup_coarse_buckets_to_model`) | the fix (read path) |
| `tests/test_litellm/proxy/proxy_server/test_routes_model_performance.py` | coarse-bucket builder + SQL row-shape tests | regression coverage |
| `_DB_CACHE_TTL` (same file, ~line 60) | window → cache TTL | caching |
| `_HEAVY_RANGE_THRESHOLD_SECONDS` (same file) | 14d → long-timeout client | large-range guard |

---

## What is NOT covered here

- **Write path** (pre-aggregation into 1-minute buckets) is unchanged and is
  documented in [model-performance-optimization-LOGIC-MAP.md](./model-performance-optimization-LOGIC-MAP.md).
- **Entity-scoped** views and **custom ranges under 14d** still read the raw
  spend log (unchanged).
- **Rollup-vs-raw spend-log deltas** (empty `model_group` excluded, cache-hit
  exclusion, buffered partial minute) are inherent to the rollup, not introduced
  by this fix — see the session recovery doc.