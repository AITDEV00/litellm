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

## Open / todo items to resume

- [ ] Validate the peak-concurrency SQL against real data once Postgres is up
      (DB port 5432 was DOWN at the end of the session; only the proxy on 4000 was up).
      Confirm peak-per-bucket matches expectation on overlapping request intervals.
- [ ] Decide whether to adopt partitioning (step 1 above). It's the highest-ROI next move.
- [ ] Sketch + implement the rollup table + incremental job for aggregate metrics.
- [ ] Decide whether to implement the write-time `(net, peak)` monoid for peak
      concurrency.

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