# Model Performance: Concurrency & Performance Work — Session Recovery Notes

> Use this file to get back up to speed on the Model Performance feature work after a
> gap. It records what was built, the decisions made, the current open question, and
> where to resume.

## Branch / State

- Branch: `jya0-v1.95.0`
- All changes are committed and pushed (commit and push happened at the end of this
  session).

## What this feature is

The **Model Performance** tab (`ui/litellm-dashboard/src/components/UsagePage/components/ModelPerformance/ModelPerformanceView.tsx`)
shows per-model-group timeseries for a dashboard. Backed by the endpoint
`/model/performance` in
`litellm/proxy/model_metrics_endpoints/model_performance_endpoints.py`.

Metrics per bucket:
- request count
- total completion tokens
- avg throughput (tokens/sec)
- avg / p50 / p95 TTFT
- **concurrent_requests** — this is the contentious one, see below.

Two data sources depending on window:
- **DB path** (24h / 7d / custom ranges): queries `LiteLLM_SpendLogs` directly.
- **Prometheus path** (live `1m`/`5m`/`15m`/`30m`/`1h`): queries Prometheus gauges.

## What was built this session (and prior)

1. **Realtime / Live mode**
   - New Live toggle in the UI. `LIVE_WINDOW = "5m"`, `LIVE_REFRESH_MS = 10_000`,
     react-query `refetchInterval` refreshes the Live view every 10s.
   - Live is disabled when scoped to an entity (team/org/user/end-user/api-key/agent)
     because that data lives only in the DB, not Prometheus.
   - New backend realtime windows: `1m`, `30m` added to `_VALID_WINDOWS`.

2. **Provider-prefix fix**
   - Live (Prometheus) surfaced deployment names WITH provider prefix (e.g.
     `hosted_vllm/Qwen/...`) while the DB path returned `model_group` without it.
   - Fixed with `_strip_provider_prefix()` using `litellm.provider_list`, plus
     `import litellm`. Pinned by test `test_model_performance_prometheus_strips_provider_prefix`.

3. **Historical slowness (YTD ~50s → ~24s)**
   - Root cause: the covering index `LiteLLM_SpendLogs_model_performance_covering_idx`
     was DROPPED from the DB while its migration showed as applied, so YTD fell back to
     a full table scan.
   - Migration (idempotent `CREATE INDEX IF NOT EXISTS`) ships in
     `litellm-proxy-extras/litellm_proxy_extras/migrations/20260805120000_add_spend_logs_model_performance_covering_index/migration.sql`.
     Index is on `("startTime")` INCLUDE (cache_hit, model_group, endTime,
     completion_tokens, request_duration_ms, completionStartTime).
   - Restored manually via psycopg3. Confirmed via `EXPLAIN ANALYZE`: both CTEs now
     index-only scans (~7s each), full query ~14s, cold API ~24-38s, cached ~0.04s.
   - Index WILL be deployed on k8s (migrations job runs from the same
     `litellm-proxy-extras/.../migrations/` dir).

3. **Peak concurrency change (unvalidated against real data)**
   - Live path: switched `concurrent_requests` promql from instant gauge to
     `sum by (model_id) (max_over_time(litellm_deployment_in_progress_requests{...}[<step>]))`
     so each point is the TRUE max concurrency in the interval, not a 15s-sampled
     snapshot (Prometheus scrape interval is 15s).
   - Historical path: replaced the "cumulative running count at bucket edge" with a
     TRUE peak-per-bucket computation: each request emits +1 at `startTime`, -1 at
     `endTime` (clamped via `LEAST(endTime, $2)`), running `SUM(change) OVER`, then
     `MAX` per `date_bin` bucket via `GREATEST(MAX(cumulative), 0)`.
   - This ADDS a third pass + a window sort over the range (see the open question).

## Open question — "is the index the best approach?"

This is where we left off. The user asked whether the covering index is the best
solution to the performance issue. Research (done this session) concluded:

**No — the index is the best FIRST approach, not the best approach.** The covering
index fixes scan cost but cannot remove the peak-concurrency window sort (an
order-sensitive, interval-spanning computation that is not a GROUP BY aggregate), nor
does it help when the log grows to tens of millions of rows.

## RESOLVED — "1 instead of 0" peak concurrency root cause + fix

The user reported that, for peak concurrency, models that were NOT called in the
window still displayed **1** instead of **0**. This was investigated live and fixed.

### Symptom (confirmed from live Prometheus, localhost:9090)

For `hosted_vllm/nvidia/diffusiongemma-26B-A4B-it-NVFP4` (model_id
`67ee7098-...`), the `litellm_deployment_in_progress_requests` gauge showed:

```
pod hqhxl (10.42.6.224):  +1   <-- has api_base label
pod 9g89n (10.42.6.223):   0
pod hqhxl (10.42.6.224):  -1   <-- NO api_base label
```

So a model that was NOT called still surfaced `5m_peak = 1.0`, and several models
showed negative instant values (-625, -1157, -344, -1).

### Root cause (two compounding defects in the gauge)

1. **Label-mismatch desync (the "1" mechanism).** `litellm_deployment_in_progress_requests`
   is a `multiprocess_mode="livesum"` gauge driven by bare `.inc()` (pre-call hook) and
   `.dec()` (success/failure metrics). The inc and dec paths each resolve their label
   tuple from *different* source chains (inc reads `custom_llm_provider`/`api_base` from
   metadata; dec reads from `llm_provider`/`litellm_params`). When they disagree (e.g.
   one path resolves `api_base` and the other does not, or a provider-prefix mismatch),
   the inc and dec hit **different series** for the same `model_id` that never cancel.
   The `+1` series stays stuck at 1 forever, so an uncalled model reports 1. The `-1`
   series is the matching dec that never found its inc.
2. **No reconciliation / no floor.** Even a correct single series leaks if a request
   spans a restart (or an exception path skips `dec`): the gauge has no reset and no
   `>= 0` clamp at write time, so drift is permanent.

### Fix — `_DeploymentInFlightLedger` in `litellm/integrations/prometheus.py`

Replace bare `.inc()/.dec()` with an authoritative **per-process in-flight ledger**
keyed by `model_id` (the stable deployment identity present and consistent in both the
pre-call and post-call payloads):

- `_DeploymentInFlightLedger.reconcile(model_id, name, base, provider, delta, emit)`
  applies `+1`/`-1`, clamps the count to `>= 0`, and **`set()`s** the gauge (never
  inc/dec) from the ledger count.
- It keeps a canonical label tuple per `model_id` and **resets every previously
  emitted stale series back to 0** so a divergent-label series can never linger as a
  phantom nonzero.
- Reconcile + emit happen under a lock, so concurrent inc/dec can't interleave
  wrongly.
- All three call sites route through `_reconcile_deployment_in_flight()`:
  `_inc_deployment_in_progress` (+1), `set_llm_deployment_failure_metrics` (-1),
  and the success-metrics dec (-1).

### Why this fixes it

- **Phantom 1 is gone**: the gauge is derived from a count keyed by `model_id`, not
  from accumulating `.inc/.dec` across possibly-divergent label tuples. Any stale
  series for a `model_id` is forced to 0.
- **Negative is impossible**: the count is clamped `>= 0`.
- **Works with 4 granian workers**: the deployment runs granian with
  `--num_workers 4` and no `PROMETHEUS_MULTIPROC_DIR`, so each worker owns a process
  local gauge; the ledger is per-process and matches that model.

### Tests

Extended `tests/test_litellm/integrations/test_prometheus_deployment_in_progress_requests.py`
(17 tests pass):

- `test_label_divergence_self_heals_no_phantom_one`: inc with `api_base`, dec with empty
  `api_base` (the exact live scenario) -> gauge returns to 0, no nonzero series remains.
- `test_gauge_clamps_at_zero_when_dec_outnumbers_inc`: a stray dec cannot push the
  gauge negative.

### Recommended layered solution (cost-to-value)

1. **Turn on native time partitioning** — the runbook ALREADY SHIPS in
   `db_scripts/partition_spend_logs.sql` + `spend_logs_partition_manager.py`
   (partition on `startTime`). Highest ROI, lowest effort. Shrinks 24h/7d scans, free
   retention (drop partition). This is the first thing to revisit.
2. **Pre-aggregate the easy metrics** (count, sum tokens, avg throughput, avg/p50/p95
   TTFT) into a small rollup table queried instead of the raw log. TTFT percentiles
   aren't exactly mergeable; keep per-bucket count/sum/sum_sq/min/max + bounded sample
   set for near-exact percentiles.
3. **For peak concurrency, maintain a write-time per-bucket monoid** `(net, peak)`:
   - `net` = starts up to bucket end minus ends up to bucket end
   - `peak` = max running-sum within the bucket from 0
   - Combine rule: `combined.peak = max(a.peak, a.net + b.peak)`, `combined.net = a.net + b.net`.
   - This is the only way to make peak concurrency incrementally pre-aggregatable /
     sub-100ms. Don't try to force it into a read-path GROUP BY aggregate.
4. Keep the `startTime` B-tree for raw drilldowns. Only add BRIN if B-tree size or
   full-year scans dominate AFTER steps 1-3.

Equivalent all-in-one alternative: TimescaleDB continuous aggregates (requires the
extension + a custom aggregate for the `(net, peak)` monoid).

## "How easy is it to create a new table?" — feasibility assessment

The user asked how hard it is to create a new rollup table **in the current DB
connection** to cut the Model Performance page load time. Answer: **low friction** —
the repo already has the exact pattern to copy.

### The mechanism is proven in-repo

Write-time aggregation already exists and works today:

- `DailySpendUpdateQueue` (in
  `litellm/proxy/db/db_transaction_queue/daily_spend_update_queue.py`) accumulates
  per-request deltas in memory and flushes **aggregated** rows to the daily tables.
- Those tables (`LiteLLM_DailyUserSpend`, `LiteLLM_DailyTeamSpend`) are created by
  timestamped `migration.sql` files under
  `litellm-proxy-extras/litellm_proxy_extras/migrations/<timestamp>_<name>/`.
- `ProxyExtrasDBManager.setup_database()` runs every pending migration on startup
  (`use_v2_migration_resolver`), and the proxy is started with
  `--use_v2_migration_resolver`, so a new migration is applied automatically on the
  next rollout. No manual DDL needed.

So adding a rollup table = **one new `migration.sql`** (CREATE TABLE IF NOT EXISTS +
indexes) **+ a write-time updater** mirroring `DailySpendUpdateQueue`.

### Proposed rollup schema (scratch sketch)

```sql
CREATE TABLE IF NOT EXISTS "LiteLLM_ModelPerformanceRollup" (
    "model_group"    TEXT NOT NULL,
    "bucket_start"   TIMESTAMPTZ NOT NULL,
    "bucket_end"     TIMESTAMPTZ NOT NULL,
    "request_count"  BIGINT NOT NULL DEFAULT 0,
    "completion_tokens" BIGINT NOT NULL DEFAULT 0,
    "throughput_sum" DOUBLE PRECISION NOT NULL DEFAULT 0,   -- token-seconds, for avg
    "ttft_sum"       DOUBLE PRECISION NOT NULL DEFAULT 0,   -- for avg TTFT
    "ttft_sum_sq"    DOUBLE PRECISION NOT NULL DEFAULT 0,   -- for p95 (approx)
    "ttft_min"       DOUBLE PRECISION,
    "ttft_max"       DOUBLE PRECISION,
    "peak_net"       BIGINT NOT NULL DEFAULT 0,             -- (net, peak) monoid
    "peak_peak"      BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY ("model_group", "bucket_start")
);
CREATE INDEX IF NOT EXISTS idx_mp_rollup_time ON "LiteLLM_ModelPerformanceRollup" ("bucket_start");
```

The `(peak_net, peak_peak)` pair is the write-time monoid from step 3 above:
`combined.peak = max(a.peak, a.net + b.peak)`, `combined.net = a.net + b.net`. This is
what makes **peak concurrency pre-aggregatable** (a read-path GROUP BY can't compute a
true peak). Entity-scoped queries would add an `entity_type`/`entity_id` split if the
per-entity page needs it too.

### Trade-off / what NOT to do

- Do **not** make the rollup the only read path for the live windows (`1m`-`1h`) —
  Prometheus is already fast there and the live view polls every 10s; the rollup
  targets the `24h`/`7d`/custom-range DB path that currently does the window sort.
- Percentiles (p50/p95 TTFT) are **not exactly mergeable** across sub-intervals. For
  near-exact results keep `sum/sum_sq/min/max` + a bounded per-bucket sample set, or
  accept the approximation. The `(net, peak)` monoid is the ONLY metric that is exact
  when aggregated.

## Open / todo items to resume

- [x] **VALIDATE + FIX the peak-concurrency gauge** (see "1 instead of 0" section
      above). Done this session: `_DeploymentInFlightLedger` + tests.
- [ ] Decide whether to adopt partitioning (step 1 above). It's the highest-ROI next move.
- [ ] Sketch + implement the rollup table + incremental job (see feasibility section
      above). The migration-based path is proven low-friction.
- [ ] Implement the write-time `(net, peak)` monoid for peak concurrency as part of
      the rollup table.

## How to bring up local env

- **Postgres**: `DATABASE_URL=postgresql://litellm:litellm_proxy_2025@127.0.0.1:5432/litellm`
  (port 5432, was down at session end — bring up via docker compose in the workspace root).
- **Redis**: `REDIS_HOST=127.0.0.1`, `REDIS_PORT=16379`, `REDIS_PASSWORD=litellm-redis-7f3a9b2e`.
- **Prometheus**: `PROMETHEUS_URL=http://127.0.0.1:9090` (scrape_interval 15s).
- **Proxy** (port 4000):
  `nohup .venv/bin/litellm --config oicm-litellm-layer/config/local_datasource.yaml --port 4000 > /tmp/litellm.log 2>&1 &`
  env: `LITELLM_MASTER_KEY={{ master_key }}`, `STORE_MODEL_IN_DB=true`, `PYTHONPATH=litellm/proxy`.
- **Dashboard dev server** (port 3000): `npm run dev` in `ui/litellm-dashboard`.
- DB access: `.venv/bin/python` with psycopg3 (`import psycopg`) — psql NOT available,
  psycopg2 NOT installed.

## Test commands

- `cd /home/jyao/ADEO/service/litellm`
- `.venv/bin/python -m pytest tests/test_litellm/proxy/proxy_server/test_routes_model_performance.py -q`
- 12 tests pass after the peak-concurrency change.

## Key files

- `litellm/proxy/model_metrics_endpoints/model_performance_endpoints.py` — the endpoint.
- `litellm/integrations/prometheus_helpers/prometheus_api.py` — Prometheus query + `_WINDOW_CONFIG`.
- `litellm/integrations/prometheus.py` — emits `litellm_deployment_in_progress_requests` gauge (line ~471, `multiprocess_mode="livesum"`).
- `ui/.../ModelPerformance/ModelPerformanceView.tsx` — the tab UI.
- `ui/.../hooks/models/useModelPerformance.ts` — react-query hook (live refetch).
- `tests/test_litellm/proxy/proxy_server/test_routes_model_performance.py` — tests.
- `litellm-proxy-extras/.../migrations/20260805120000_.../migration.sql` — the covering index migration.
- `db_scripts/partition_spend_logs.sql` — the EXISTING partitioning runbook (step 1 of the recommended plan).