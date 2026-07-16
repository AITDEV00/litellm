# Dashboard Extension Proposal: Usage by Model and API

## Decision Document

Status: draft, awaiting approval before any code is written.
Owner: proxy / dashboard team.
Scope: extend the LiteLLM admin dashboard so an operator can see, at a glance, where load is coming from and how it is distributed across models and replicas.

---

## 1. What you asked for

You want the dashboard's "usage by model and API" view to be richer. Two specific capabilities were named:

1. Concurrent request counts (live or near-live).
2. Requests per model per replica over time (time-series breakdown).

You also asked whether such a view would surface "where the majority of the load is coming from" at a glance. And you wanted an assessment of how hard the frontend is to modify.

---

## 2. Current state of the LiteLLM proxy dashboard

### 2.1 What already exists in the backend

The proxy exposes these metrics endpoints in `litellm/proxy/proxy_server.py`:

| Endpoint | Returns | Per-replica? | Time-series? |
|----------|---------|--------------|--------------|
| `GET /model/streaming_metrics` | time-to-first-token, grouped by `(api_base, model)` | yes | per-request when same-day, daily otherwise |
| `GET /model/metrics` | avg latency per token, grouped by `(api_base, model, day)` | yes | daily buckets |
| `GET /model/metrics/slow_responses` | slow_count vs total_count per `api_base` | yes (api_base only) | no |
| `GET /model/metrics/exceptions` | exception counts per `(model, api_base)` | yes | no |
| `GET /metrics` | full Prometheus scrape | yes (`model_id` + `api_base` labels) | yes (`rate()`, `increase()`) |
| `Router.get_model_group_usage()` | current minute TPM/RPM per deployment | yes | real-time (in-memory) |

The spend-log table itself (`LiteLLM_SpendLogs` in `schema.prisma`, lines 581-617) has every field needed for per-replica time-series queries: `request_id`, `model`, `model_id`, `model_group`, `api_base`, `startTime`, `endTime`, `completionStartTime`, `request_duration_ms`, plus indexes on `[startTime, request_id]` and `[end_user]`.

### 2.2 What the dashboard actually renders today

Almost none of the above is rendered. Grepping `ui/litellm-dashboard/src` for `/model/metrics`, `/model/streaming_metrics`, `/model/metrics/slow_responses`, and `/model/metrics/exceptions` returns matches only in the auto-generated `schema.d.ts`. There is no `modelMetricsCall`, `streamingMetricsCall`, or `slowResponsesCall` in `src/components/networking.ts`. The dashboard page at `src/app/(dashboard)/models-and-endpoints/` is built only from `GET /model/info` and `GET /model/group/info`. The Usage page at `src/app/(dashboard)/usage/` uses the `/spend/logs` endpoints plus `/user/daily/activity` and `/team/daily/activity`.

### 2.3 Gaps relative to what you asked for

1. No true concurrent-requests counter. Searching `litellm/integrations/prometheus.py` for `Gauge` and `_active_requests`/`in_progress`/`concurrent` shows only budget gauges, rate-limit-remaining gauges, deployment limits, deployment state, and user/team/org counts. There is no `litellm_concurrent_requests` or `litellm_requests_in_progress` counter. The closest real-time signal is `litellm_remaining_requests_metric` (RPM left in the current minute window), which is derived from the dual-cache counters in `litellm/router_utils/pre_call_checks/model_rate_limit_check.py`. That is not the same as live concurrency.
2. No requests-per-replica-over-time chart. The Prometheus labels confirm per-replica breakdown is possible (`model_id` + `api_base` on `litellm_deployment_total_requests` and `litellm_deployment_latency_per_output_token`), but no Prometheus query of that shape is exposed as an endpoint and no UI consumes `/metrics`.
3. No "top consumers" view. There is no aggregate that ranks API keys, teams, or users by live or recent load. The spend logs have the data but no rolled-up endpoint exists.
4. "Replica" in LiteLLM means one deployment entry, which maps to one `api_base` + one `model_id`. There is no pod- or host-level identity in the data model. If you want to see per-pod metrics, those need to come from vLLM or Kubernetes, not the proxy.

---

## 3. What a "proper" LLM-gateway dashboard should contain

Based on public references (Langfuse, Portkey, Helicone, Datadog LLM Observability, Kong AI Gateway guide), the consistent set of widgets operators expect is:

### 3.1 Real-time operational view (last 5 minutes, auto-refresh)

- Live request rate (RPS) by model and by api_base
- Live token rate (TPS) by model and by api_base
- Live concurrent requests by model and by api_base
- Live RPM/TPM utilization against configured limits (RPM used / RPM limit per deployment)
- P50 / P95 / P99 latency by model and by api_base
- P50 / P95 / P99 time-to-first-token for streaming endpoints
- Error rate by status code and exception class
- Number of deployments in healthy / partial outage / complete outage

### 3.2 Top consumers view (last 1h / 24h / 7d)

- Top 10 API keys by request volume, token volume, spend, and concurrent share
- Top 10 teams by the same four metrics
- Top 10 end users by the same four metrics
- Top 10 client IPs by the same four metrics
- Treemap or stacked-bar showing proportional load share

### 3.3 Per-replica time-series view (last 24h, bucket = 1 minute)

- RPM over time per deployment (`api_base + model_id`)
- TPM over time per deployment
- P95 latency over time per deployment
- Error rate over time per deployment
- Fallback events (primary deployment -> fallback deployment)
- Cool-down events (deployment temporarily disabled by the router)

### 3.4 Anomaly and SLO indicators (last 24h / 7d)

- Slow-request share per deployment (calls above the alerting threshold)
- Exception rate per deployment broken down by exception class
- Cache hit rate by model
- Spend rate in USD/hour

### 3.5 Drill-down

Each card should link through to a filtered spend-logs view (`/spend/logs?api_key=...&model=...&startTime=...`) so the operator can find the individual requests that produced a spike.

The "at a glance" view is the combination of 3.1 and 3.2. A single landing tab should answer three questions in under three seconds: how loaded are we right now, where is the load coming from, and is anything degraded.

---

## 4. Implementation plan (three tiers)

These tiers are independent. You can ship one without the others.

### Tier 1 — Read the existing endpoints from a new dashboard view (small)

Pure frontend work. No backend changes.

- Add four wrappers to `src/components/networking.ts`: `streamingMetricsCall`, `modelMetricsCall`, `slowResponsesCall`, `exceptionsMetricsCall`.
- Add a new page at `src/app/(dashboard)/models-and-endpoints/analytics/page.tsx` (or a new tab inside the existing `ModelsAndEndpointsView`).
- Render the four existing series with the existing `@tremor/react` chart components (`BarChart`, `LineChart`, `AreaChart`) plus a top-N bar list for slow responses and exceptions.
- Reuse the `ChartLoader` skeleton at `src/components/shared/chart_loader.tsx`.
- Reuse `usePaginatedDailyActivity` and `tanstack/react-query` for data fetching.

Limitations: still no concurrency counter; time-bucket granularity is whatever the backend query chooses (daily or per-request).

Effort: 2-4 days for one engineer.

### Tier 2 — Add a real concurrent-requests counter (medium)

Backend change plus a dashboard widget.

Backend:
1. Add a `Gauge` in `litellm/integrations/prometheus.py`, e.g. `litellm_deployment_in_progress_requests`, labeled with `model_id`, `api_base`, `model_group`, `api_provider`.
2. Increment on entry to the router call path and decrement in a `finally` block so streaming / timeout / 5xx / cancelled requests are released. Hook points: `litellm/router.py` around `async def acompletion`, and the parallel-execution fan-out paths in `litellm/router_utils/`.
3. Add `litellm_deployment_in_progress_requests` to the label set in `litellm/types/integrations/prometheus.py` so it lands on `/metrics` automatically.
4. Add a new endpoint `GET /model/metrics/concurrent_requests` in `litellm/proxy/proxy_server.py` that returns `max_over_time(...)` / `avg_over_time(...)` from Prometheus if `PROMETHEUS_URL` is set, otherwise returns the in-memory counter directly. Mirror the signature of `model_metrics_slow_responses` for consistency.
5. Add a unit test that exercises the inc/dec contract with a fake router call so a regression (a code path that exits without decrement) fails CI.

Frontend:
- New card on the analytics page showing live concurrent requests per deployment with auto-refresh every 5s.
- A "concurrency vs limit" gauge so you can see headroom.

Effort: 5-7 days for one engineer including tests and staging validation. The risk is in the inc/dec contract; one missed decrement causes the gauge to climb forever.

### Tier 3 - Add a "top consumers" rolled-up endpoint + view (small-to-medium)

Backend:
1. New endpoint `GET /spend/top_consumers` (or `/usage/top_consumers`) that runs a `GROUP BY` over `LiteLLM_SpendLogs` for the selected window and returns rows of `{dimension, dimension_alias, request_count, total_tokens, prompt_tokens, completion_tokens, spend}`. The default and primary dimension is `api_key`; the endpoint accepts `dimension=team_id|user_id|end_user` for future extension. First cut ships API key only.
2. Optional second endpoint for the time-series view: `GET /spend/timeseries` with parameters `dimension`, `bucket` (1m, 5m, 1h, 1d), `window`, and the same set of grouping keys.
3. Tests for both endpoints with seeded spend logs so regressions in the SQL are caught.

Frontend:
- New "Top consumers" section inside the existing "Key Activity" tab (or as its own sibling tab) on the Usage page. A sortable table plus a stacked-bar chart of share by API key.
- A single "Where is the load coming from?" treemap on the Usage page landing area, sized by request volume and colored by error rate.

Effort: 3-5 days for one engineer.

### Combined effort

Tier 1 + Tier 2 + Tier 3 = roughly 10-16 working days for one engineer, assuming no surprises. Reasonable to ship Tier 1 first as a low-risk demonstrator, then evaluate Tier 2 and Tier 3 separately.

---

## 5. Frontend modification difficulty assessment

### 5.1 Stack

- Next.js (app router, see `src/app/(dashboard)/.../page.tsx`)
- React, TypeScript strict
- Ant Design 5.29 for layout / forms / tables
- `@tremor/react` 3.18 for charts (Bar, Line, Area, Donut, ProgressBar, Tracker, Metric, Card, Title, Text, Grid, Col)
- `@tanstack/react-query` 5.100 for data fetching and caching
- `axios` 1.13 for HTTP
- `date-fns` 3.6 and `dayjs` 1.11 for dates
- Playwright for e2e, Vitest for unit

### 5.2 Patterns already in use

- Charts: `import { BarChart, Card, Title } from "@tremor/react"` is the dominant pattern (222 imports of `@tremor/react`). Real example at `src/components/UsagePage/components/UsagePageView.tsx:681-774`.
- Data fetching: a network wrapper in `src/components/networking.ts`, consumed by hooks under `src/components/UsagePage/hooks/` (`usePaginatedDailyActivity.ts`) that wrap `react-query` for pagination.
- New pages live in `src/app/(dashboard)/<feature>/page.tsx` and follow the existing dashboard sidebar entry pattern.
- Loading skeletons: `src/components/shared/chart_loader.tsx`.
- Tests: Vitest unit tests colocated with components, Playwright e2e tests in `e2e_tests/tests/`.

### 5.3 Difficulty ratings

| Change | Difficulty | Why |
|--------|-----------|-----|
| Add a new dashboard page that calls 4 existing endpoints | Easy | Follow the existing `networking.ts` -> `usePaginatedDailyActivity.ts` -> `UsagePageView.tsx` pattern verbatim |
| Add a new tab inside `ModelsAndEndpointsView` | Easy | The view already has tabs; pattern in `UsageViewSelect.tsx` |
| Add a new `@tremor/react` chart type | Trivial | Drop-in component, copy an existing example |
| Add a new top-consumers view with table + treemap | Easy-Medium | Table is Ant Design; treemap is `@tremor/react` `DonutChart` or stacked `BarChart` |
| Add a new endpoint and a wrapper for it | Easy | One function in `networking.ts`, one hook, one consumer |
| Add the concurrent-requests gauge and dashboard card | Medium-Hard | Backend work is non-trivial; frontend card itself is trivial |

The hardest part of frontend work is not the charting. It is matching the strict TypeScript and lint rules (the repo has `ruff-strict`, `type-discipline-budget`, and `basedpyright-code-budget` files, suggesting tight ceilings on `Any`, `explicit Any`, and similar). Existing tests run through `vitest` and must pass. `make pre-commit` must succeed before each commit. Treat these as build-blocking constraints.

### 5.4 Risk areas

1. The auto-generated `src/lib/http/schema.d.ts` is produced by `node scripts/gen-api-types.mjs`. Adding a new backend endpoint requires regenerating that file, otherwise the frontend type-check fails.
2. There are lint budgets that ratchet down. New code must respect them.
3. The repo's CLAUDE.md forbids comments unless explicitly requested, and the GitHub PR template conventions are strict. Plan to match the existing house style, not invent a new one.

---

## 6. Open questions - answered

The questions below were originally open; the decisions in this section reflect the agreed direction after discussion.

### Q1. Which proxy / branch?

All code changes (backend and frontend) go on the `jya0-v1.92.0` branch directly. No split between staging and production branches; this is the ADEO fork branch and it is what gets built into the proxy image. Tier 2's in-memory counter has no external dependency, so it works regardless of which proxy runs it.

### Q2. Is there an existing Prometheus?

Yes, and this changes the Tier 2 plan. The ADEO cluster runs a full `kube-prometheus-stack` (Prometheus + Grafana + Alertmanager + node-exporters, 2 Prometheus replicas, 215+ days uptime). Confirmed on 2026-07-16:

```
kubectl get pods -A | grep prometheus
kube-prometheus-stack  prometheus-kube-prometheus-stack-prometheus-0   2/2 Running  209d
kube-prometheus-stack  prometheus-kube-prometheus-stack-prometheus-1   2/2 Running  209d
```

What "Prometheus" means here: Prometheus is a time-series database that scrapes (pulls) metrics from targets at a fixed interval (30s by default) and stores them on disk with configurable retention. It is the storage layer for metrics history. Grafana (also running in the cluster) is the visualization layer that queries Prometheus to draw dashboards. The proxy currently does not emit to it, but the storage and scrape infrastructure already exists.

Current state:
- The litellm-proxy service exists in `mlops` namespace (`ClusterIP 10.43.188.139:4000`).
- There is NO ServiceMonitor for litellm-proxy, so Prometheus is not scraping it.
- The proxy's `/metrics` endpoint returns 404 because the `prometheus` callback is not in `litellm_settings.callbacks` and `PROMETHEUS_URL` is not set.

So there are two independent gaps to close if we want Prometheus-backed metrics:
1. Proxy config: add `prometheus` to `litellm_settings.callbacks` so the `/metrics` endpoint comes alive and the litellm Prometheus metrics get registered.
2. Cluster config: add a ServiceMonitor in the `mlops` namespace that scrapes `litellm-proxy:4000/metrics` on a 30s interval. The existing `oicm-api-gateway-service-monitor` in the same namespace is the reference pattern (it scrapes `oicm-api-gateway:8080/prometheus-metrics`).

Both are small. With them in place, Tier 2 can ship the full Prometheus path: the new `litellm_deployment_in_progress_requests` Gauge gets scraped automatically, Prometheus stores its history, and the dashboard endpoint can query Prometheus for `max_over_time` / `avg_over_time` for the time-series chart. The in-memory counter remains as a fallback for when `PROMETHEUS_URL` is not set.

Recommendation: wire up Prometheus. The infrastructure is already paid for and running. Without it, Tier 2 loses the time-series view (you only get the current instant value). With it, you get both instant concurrency and historical concurrency trends.

### Q3. Per-pod visibility?

Out of scope. A registered model is treated as one replica. If multiple pods back the same `model_name`, the proxy still sees them as a single deployment, identified by `model_id` + `api_base`. This is the correct granularity for the proxy layer.

### Q4. Top-consumer dimension

API key is the agreed dimension for Tier 3. The new endpoint should accept `dimension=api_key` as the default, with `team_id`, `user_id`, and `end_user` as optional alternatives for future extension. UI should land on API key for the first cut.

### Q5. Where does the new view live?

The new view goes inside the Usage page as a new tab, not inside Models and Endpoints. Rationale:

- The Usage page is already scoped around "who is doing what, with which model, when" - the natural home for per-replica analytics.
- `UsagePageView.tsx` already has a `TabGroup` with five tabs (Cost, Model Activity, Key Activity, MCP Server Activity, Endpoint Activity). A new tab "Model Performance" or "Replicas" slots in naturally.
- Putting it inside Models and Endpoints would duplicate the conceptual model (you would have to manage two filter contexts) and would not benefit from the existing date range / user picker / export pipeline that the Usage page already wires up.
- The "Top consumers" view (Tier 3) also goes inside Usage, as a sub-section of the existing "Key Activity" tab or as its own sibling tab.

Target file: `ui/litellm-dashboard/src/components/UsagePage/components/UsagePageView.tsx`. Insert a new `<Tab>` after `<Tab>Endpoint Activity</Tab>` (line 531) with its own `<TabPanel>` containing the new component.

---

## 7. Recommendation

Ship Tier 1 first. It is low-risk, exercises the existing data, and proves the dashboard integration pattern. Then evaluate Tier 2 and Tier 3 based on operator feedback. Do all three together if there is a hard deadline and a clear owner.

### Revised tier estimates

Prometheus infrastructure already exists in the cluster; we just need to enable the proxy callback and add a ServiceMonitor. That wiring is ~1 day of cluster/config work, and it unlocks the full Tier 2 time-series path.

| Tier | Description | Effort |
|------|-------------|--------|
| 0 | Enable `prometheus` callback in proxy config + add ServiceMonitor in `mlops` namespace (reference: `oicm-api-gateway-service-monitor`) | 1 day |
| 1 | New Usage tab consuming the 4 existing endpoints | 2-4 days |
| 2 | Concurrent-requests Gauge + endpoint + dashboard card (full Prometheus path now viable) | 5-7 days |
| 3 | Top-consumers endpoint (API key dimension) + treemap | 3-5 days |
| **Total** | | **11-17 days** for one engineer |

Reference files for any implementation:

- `litellm/proxy/proxy_server.py:11865-12260` - existing metrics endpoints to mirror
- `litellm/router.py:8896-8935` - `get_model_group_usage` for real-time RPM/TPM
- `litellm/router_utils/pre_call_checks/model_rate_limit_check.py:118-225` - RPM/TPM counter location (Tier 2 hook point)
- `litellm/integrations/prometheus.py:336-430` - Gauge definitions (Tier 2; Prometheus not required to use them)
- `litellm/types/integrations/prometheus.py:416-450` - per-replica label sets
- `litellm/integrations/prometheus_helpers/prometheus_api.py` - Prometheus query helper (out of scope for ADEO; no Prometheus)
- `schema.prisma:581-617` - `LiteLLM_SpendLogs` schema
- `ui/litellm-dashboard/src/components/networking.ts` - add new API wrappers here
- `ui/litellm-dashboard/src/components/UsagePage/components/UsagePageView.tsx:524-560` - existing tab list to extend
- `ui/litellm-dashboard/src/components/UsagePage/components/UsagePageView.tsx:681-774` - BarChart pattern to copy
- `ui/litellm-dashboard/src/components/UsagePage/hooks/usePaginatedDailyActivity.ts` - pagination hook pattern to copy
