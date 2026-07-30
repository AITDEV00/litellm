# Observability Implementation Plan: Per-Request & Per-Model Metrics

Status: proposal.
Date: 2026-07-30.

This document applies the frontend understanding technique to design an
observability feature for the LiteLLM dashboard. It covers:

1. What metrics to show per-request (throughput, TTFT, total time)
2. What metrics to show per-model (throughput, concurrent, TTFT over time)
3. How to make the renders performant and fast

It builds on the research findings from exploring the backend and frontend
codebases (documented inline below).

---

## Part 1: What already exists (the inventory)

Before designing anything, the technique says to map the API surface and
identify reuse opportunities. Here is what the codebase already provides.

### 1.1 Backend: data that is already captured

Every LLM request goes through the `Logging` object
(`litellm/litellm_core_utils/litellm_logging.py`). Two timestamps are real
attributes on the Logging class; the other two live as keys in the
`model_call_details` dict:

| Timestamp | Where it lives | Set when | Used for |
|-----------|---------------|----------|----------|
| `start_time` | `self.start_time` (Logging attr, `litellm_logging.py:344`) | Logging init (request begins) | Total time start |
| `api_call_start_time` | `model_call_details["api_call_start_time"]` (dict key, `litellm_logging.py:988`); NOT a Logging attribute | In `pre_call`, just before the provider HTTP call; overwritten on every retry (only `first_api_call_start_time` is set-once) | TTFT start (in-memory callbacks only) |
| `completion_start_time` | `self.completion_start_time` (Logging attr, `litellm_logging.py:376`) | First streamed chunk (streaming only); force-set to `end_time` for non-streaming (`litellm_logging.py:1813-1815`) | TTFT end |
| `end_time` | `model_call_details["end_time"]` (dict key, `litellm_logging.py:1819`); NOT a Logging attribute | Response complete | Total time end |

From these, three derived metrics are computed:

| Metric | Formula | Where it's stored |
|--------|---------|-------------------|
| **Total request time** | `end_time - start_time` | `LiteLLM_SpendLogs.request_duration_ms` (DB column, int ms; computed at `spend_tracking_utils.py:474`) |
| **TTFT** (streaming only) | **Two conflicting definitions exist.** In-memory callbacks (Prometheus `prometheus.py:1867-1873`, OTEL `opentelemetry.py:1484-1495`) use `completion_start_time - api_call_start_time` (excludes preprocessing). DB read queries (`spend_management_endpoints.py:2009-2014`, `proxy_server.py:11903`, `lowest_latency.py:80`) use `completion_start_time - start_time` (includes preprocessing). `api_call_start_time` is never persisted, so DB TTFT cannot use it. The two definitions disagree by the preprocessing overhead. | `LiteLLM_SpendLogs.completionStartTime` (DB column, DateTime; TTFT is computed on read) |
| **Tokens per second** | `completion_tokens / (end_time - start_time in seconds)` | Not stored; must be derived |

### 1.2 Backend: Prometheus metrics already emitted

The proxy emits these Prometheus metrics on every request
(`litellm/integrations/prometheus.py`):

| Metric | Type | What it measures |
|--------|------|-----------------|
| `litellm_request_total_latency_metric` | Histogram | End-to-end request latency |
| `litellm_llm_api_latency_metric` | Histogram | LLM API call latency (excludes proxy overhead) |
| `litellm_llm_api_time_to_first_token_metric` | Histogram | TTFT (streaming only) |
| `litellm_deployment_in_progress_requests` | Gauge | Concurrent in-flight requests per deployment |
| `litellm_deployment_total_requests` | Counter | Total requests per deployment |
| `litellm_output_tokens_metric` | Counter | Output tokens (label schema differs from deployment metrics: uses v1 `model` + `model_id`, lacks `api_base`) |
| `litellm_deployment_latency_per_output_token` | Histogram | Latency / completion_tokens per deployment |

### 1.3 Backend: existing endpoints

| Endpoint | Source | What it returns | Time resolution |
|----------|--------|-----------------|----------------|
| `GET /model/metrics/per_model` | Prometheus | concurrent, request_rate, output_tokens_per_sec, latency_per_token_p50 | 15s/30s/5m/1h steps |
| `GET /model/metrics` | DB | avg latency_per_token per model per day | Daily |
| `GET /model/streaming_metrics` | DB | TTFT per model (per-request or daily avg) | Per-request or daily |
| `GET /model/metrics/slow_responses` | DB | count of slow requests per `api_base` within a model_group (model_group is a WHERE filter, not a GROUP BY) | Aggregated |
| `GET /spend/logs/ui` | DB | Per-request logs with `request_duration_ms`, `completionStartTime` | Per-request |

File path correction: the spend management endpoints live at
`litellm/proxy/spend_tracking/spend_management_endpoints.py` (not
`litellm/proxy/spend_management_endpoints.py` as referenced in some older
docs).
| `GET /global/activity/model` | DB | Daily requests + tokens per model | Daily |

### 1.4 Frontend: what exists today

- **Logs table** (`view_logs/columns.tsx`): Already shows `Duration (s)` and
  `TTFT (s)` columns per request, both sortable. This is the per-request
  observability that already works.
- **Log detail drawer** (`LogDetailsDrawer/LogDetailContent.tsx`): Shows
  Duration, TTFT, Tokens, Cost per single request.
- **Usage page** (`UsagePage/`): Shows spend, token counts, request counts per
  day. No latency/throughput/timing data anywhere in `SpendMetrics`.
- **Usage views**: 9 views via `UsageViewSelect`, all spend/token focused.
- The Model Analytics and Real-Time Per Model tabs were removed (commit
  `d8324d2ca4`) because the data was broken. The frontend wrappers
  (`perModelMetricsCall`, `modelStreamingMetricsCall`, etc.) and the components
  (`ModelAnalyticsView`, `PerModelRealTimeView`) no longer exist.

### 1.5 Deployment: Prometheus is available

The k8s production deployment sets:
```
PROMETHEUS_URL=http://kube-prometheus-stack-prometheus.kube-prometheus-stack:9090
```
A `ServiceMonitor` scrapes the proxy's `/metrics` endpoint every 30s. So
Prometheus-backed real-time queries are available in production. The dev
docker-compose also runs a Prometheus container.

---

## Part 2: Why the previous implementation was broken

The removed tabs failed because of three root causes:

### 2.1 The Prometheus-backed endpoint returned deployment-level data, not model-group-level

`/model/metrics/per_model` groups by `model_id` (the internal deployment
identifier), not by `model_group` (the user-facing model name). A single
model_group like `gpt-4` can have multiple deployments (e.g. Azure + OpenAI
fallback). The frontend showed raw `model_id` values that users don't
recognize, and the label metadata recovery was unreliable (stale labels in
Prometheus).

### 2.2 The DB-backed endpoints used daily-only bucketing

`/model/metrics` and `/model/streaming_metrics` use
`DATE_TRUNC('day', "startTime")`. There is no sub-daily time bucketing anywhere
in the proxy SQL. So the "real-time" tab was actually showing daily averages,
which is not real-time at all.

### 2.3 The frontend tried to merge two incompatible data sources

The `ModelAnalyticsView` tab used DB-backed endpoints (daily latency, daily
TTFT). The `PerModelRealTimeView` tab used the Prometheus endpoint
(per-deployment, sub-minute). These two views had different data shapes,
different model identifiers, different time resolutions, and different
filtering. The result was two tabs that looked similar but showed
incomparable data, both partially broken.

---

## Part 3: Recommended approach for per-request observability

### 3.1 What to show

Per-request observability is about individual request performance. The user
wants to see, for each request:

| Metric | Source | Already shown? |
|--------|--------|---------------|
| Total time | `request_duration_ms` | Yes, in logs table + drawer |
| TTFT | `completionStartTime - startTime` | Yes, in logs table + drawer |
| Throughput (tokens/sec) | `completion_tokens / (request_duration_ms / 1000)` | No, not computed anywhere |

### 3.2 Recommendation: extend the existing logs table and drawer

The logs table already shows Duration and TTFT. The only gap is throughput.
Adding it is a small, low-risk change:

1. **Add a "Throughput" column** to `view_logs/columns.tsx`. The value is
   computed client-side: `completion_tokens / (request_duration_ms / 1000)`.
   This requires no backend change because both fields are already in the
   `LogEntry` type and the `/spend/logs/ui` response.

2. **Add throughput to the log detail drawer** in
   `LogDetailsDrawer/LogDetailContent.tsx`, next to the existing Duration and
   TTFT rows in the `MetricsSection`.

3. **Make the throughput column sortable** by adding a server-side sort option.
   This requires a backend change: add `throughput` to the sort field map in
   `spend_tracking/spend_management_endpoints.py`. The sort would compute
   `completion_tokens / NULLIF(request_duration_ms, 0)` in the SQL ORDER BY.

### 3.3 Why not a separate per-request page

A dedicated per-request observability page would duplicate the logs table. The
logs table is already the natural place for per-request data. Adding a column
there is simpler, more discoverable, and avoids the "two tabs showing similar
data" problem that broke the previous implementation.

### 3.4 Edge cases to handle

- `request_duration_ms` is 0 or null: show `-`, not `Infinity`
- `completion_tokens` is 0 (e.g. embedding requests, errors): show `-`
- Non-streaming requests: TTFT is null (`completionStartTime` equals `endTime`);
  show `-` for TTFT, but throughput is still valid
- Cache hits: `request_duration_ms` is very low; throughput will be high but
  misleading. Consider showing a "cache" badge or excluding cached requests from
  throughput calculations

---

## Part 4: Recommended approach for per-model observability

This is the harder part. The user wants, per model, over selectable time
windows (5m, 15m, 1h, 24h, 1w):

| Metric | Description |
|--------|-------------|
| Total throughput | Tokens/sec aggregated across all requests to that model |
| Concurrent requests | How many requests are in-flight right now |
| TTFT | Time to first token, trended over time |

### 4.1 The two data sources and their tradeoffs

| Source | Resolution | Latency | Historical depth | Model grouping |
|--------|-----------|---------|-----------------|----------------|
| Prometheus | 15s-1h | Real-time (30s scrape) | 15 days (config) | Per-deployment (`model_id`) |
| DB (`LiteLLM_SpendLogs`) | Per-request | Seconds to query | Unlimited (retention) | Per-model, per-model_group |

**Prometheus is the right source for real-time concurrent requests and
short-window throughput.** The gauge `litellm_deployment_in_progress_requests`
is the only source of real-time concurrency data. Token throughput via
`rate(litellm_output_tokens_metric_total[window])` is accurate for short
windows.

**The DB is the right source for TTFT trends and longer windows.** Prometheus
retention is 15 days. The DB has unlimited retention. TTFT is already stored
per-request in `completionStartTime`.

### 4.2 Recommendation: a single unified per-model view with two data tiers

Instead of two separate tabs (which was the broken approach), use a single
"Model Performance" view inside the existing Usage page. The view has a time
window selector (5m, 15m, 1h, 24h, 1w). The data source is chosen automatically
based on the window:

```
Window     Data source    What's shown
------     -----------    ------------
5m/15m     Prometheus     Real-time concurrent, throughput, p50 latency/token
                          (TTFT not available from Prometheus per-model; show
                           "live" only for concurrent + throughput)
1h         Hybrid         Prometheus for concurrent + throughput;
                          DB query for TTFT (last 1h, per-request, aggregated
                          to 1-min or 5-min buckets)
24h        DB             All three metrics from DB, bucketed to 5-min or 1-hour
1w         DB             All three metrics from DB, bucketed to 1-hour
```

### 4.3 Backend changes needed

#### 4.3.1 New endpoint: `GET /v1/model/performance`

A single new endpoint that returns all three metrics for a time window, from
the appropriate source. This avoids the "two endpoints with different shapes"
problem.

```python
@router.get("/v1/model/performance", dependencies=[Depends(user_api_key_auth)])
async def model_performance(
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    window: str = Query("1h", regex="^(5m|15m|1h|24h|1w)$"),
    model_group: Optional[str] = Query(None),
):
```

**Response structure** (unified, model_group-keyed):

```jsonc
{
  "window": "1h",
  "source": "prometheus" | "db" | "hybrid",
  "step": "30s",
  "models": [
    {
      "model_group": "gpt-4",
      "time_series": {
        "concurrent_requests": [{"timestamp": "...", "value": 3.0}, ...],
        "throughput_tokens_per_sec": [{"timestamp": "...", "value": 50.0}, ...],
        "ttft_seconds": [{"timestamp": "...", "value": 0.8}, ...]
      },
      "summary": {
        "avg_concurrent": 2.5,
        "avg_throughput": 45.0,
        "p50_ttft": 0.75,
        "p95_ttft": 1.2,
        "total_requests": 1200,
        "total_tokens": 540000
      }
    }
  ]
}
```

**Key design decisions:**

1. **Group by `model_group`**, not `model_id`. The previous implementation
   grouped by `model_id`, which exposed internal deployment identifiers. Users
   think in terms of model groups (`gpt-4`, `claude-sonnet-4`). The endpoint
   should aggregate across deployments within a model group.

2. **For Prometheus queries**, the 7 metrics listed in Section 1.2 do **not**
   carry a `model_group` label. Only 4 *other* metrics
   (`litellm_overhead_latency_metric`,
   `litellm_overhead_with_guardrails_latency_metric`,
   `litellm_remaining_requests_metric`, `litellm_remaining_tokens_metric`)
   have `model_group` (`types/integrations/prometheus.py:386,396,406,416`).
   To group Prometheus data by `model_group`, the label must first be **added**
   to the metric definitions in `types/integrations/prometheus.py` and emitted
   in `prometheus.py` via `_get_labels`. This is a code change, not just a
   query change. Alternatively, use `model_id` aggregation and join to
   `model_group` via deployment metadata (the existing
   `_get_deployment_label_metadata` approach, which is unreliable per Part
   2.1).

3. **For DB queries**, use sub-daily time bucketing. This is the critical
   missing piece. The SQL would use `date_trunc` with the appropriate
   granularity:

   ```sql
   -- For 1h window: 5-min buckets
   -- For 24h window: 1-hour buckets
   -- For 1w window: 1-hour buckets
   SELECT
     model_group,
     date_trunc('{bucket}', "startTime") AS bucket_start,
     AVG(EXTRACT(epoch FROM ("endTime" - "startTime"))) AS avg_total_time_sec,
     AVG(EXTRACT(epoch FROM ("completionStartTime" - "startTime"))) AS avg_ttft_sec,
     SUM(completion_tokens) / NULLIF(SUM(EXTRACT(epoch FROM ("endTime" - "startTime"))), 0) AS throughput_tokens_per_sec,
     COUNT(*) AS request_count,
     SUM(completion_tokens) AS total_tokens
   FROM "LiteLLM_SpendLogs"
   WHERE "startTime" >= $1 AND "startTime" <= $2
     AND "cache_hit" != 'True'
     {AND model_group = $3}
   GROUP BY model_group, bucket_start
   ORDER BY model_group, bucket_start
   ```

   The bucket size maps to the window:
   - 1h window -> `minute` bucket (5-min aligned: use `date_trunc('minute', ...) - (EXTRACT(minute FROM ...) % 5) * interval '1 minute'` or a simpler `date_trunc('hour', ...) + floor(extract(minute from ...)/5) * interval '5 min'`)
   - 24h window -> `hour` bucket
   - 1w window -> `hour` bucket

4. **For the hybrid case** (1h window), run both queries in parallel with
   `asyncio.gather`. Prometheus gives concurrent + throughput; DB gives TTFT.
   Merge by timestamp alignment.

5. **Pagination is not needed.** The number of model groups is small (typically
   10-50). The time series length is bounded by `window / step` (e.g. 1h / 30s
   = 120 points, 1w / 1h = 168 points). This is small enough to return in a
   single response.

#### 4.3.2 No new DB columns needed

All the data (`request_duration_ms`, `completionStartTime`, `completion_tokens`,
`model_group`) already exists in `LiteLLM_SpendLogs`. No migration is needed.

#### 4.3.3 No new DB tables needed

The previous broken approach tried to pre-aggregate into tables. That added
complexity and the aggregation logic was wrong. On-the-fly SQL with proper
indexing is sufficient for the expected data volume. The `startTime` index
already exists (`@@index([startTime])`).

For very high-volume deployments (millions of requests/day), a materialized
view refreshed every 5 minutes could be added later. But that is an
optimization, not a starting point. Start with on-the-fly SQL.

### 4.4 Frontend changes needed

#### 4.4.1 New API wrapper in networking.tsx

```ts
export const modelPerformanceCall = async (
  accessToken: string,
  params: { window: string; model_group?: string }
) =>
  apiClient.get("/v1/model/performance", {
    accessToken,
    query: { window: params.window, model_group: params.model_group },
  });
```

One wrapper, one endpoint, one response shape. This fixes the "two endpoints
with different shapes" problem.

#### 4.4.2 New hook

Create `src/app/(dashboard)/hooks/models/useModelPerformance.ts`:

```ts
export function useModelPerformance(window: string, modelGroup?: string) {
  const { accessToken } = useContext(AuthContext);
  return useQuery({
    queryKey: ["model-performance", window, modelGroup],
    queryFn: () => modelPerformanceCall(accessToken!, { window, model_group: modelGroup }),
    enabled: !!accessToken,
    refetchInterval: window === "5m" || window === "15m" ? 30000 : false,
    staleTime: window === "5m" || window === "15m" ? 15000 : 60000,
  });
}
```

The `refetchInterval` gives live updates for short windows (30s, matching the
Prometheus scrape interval). Longer windows don't need polling.

#### 4.4.3 New component: ModelPerformanceView

Create `src/components/UsagePage/components/ModelPerformance/ModelPerformanceView.tsx`.

Structure:
- A window selector (Segmented: 5m / 15m / 1h / 24h / 1w)
- A model selector (optional: antd Select populated from the response's
  `models[].model_group`, defaulting to "all")
- Three chart cards in a grid:
  1. **Concurrent Requests** (AreaChart or LineChart)
  2. **Throughput (tokens/sec)** (AreaChart or LineChart)
  3. **TTFT (seconds)** (LineChart with p50/p95 lines if DB-backed)
- A summary table below: one row per model_group with avg/p50/p95/total

This component plugs into the existing Usage page as a new inner tab in the
`global` view's `TabGroup`, or as a new `usageView` option. Given the existing
9 views, adding it as an inner tab in the global view is less disruptive.

#### 4.4.4 Chart library choice

The existing usage page uses `@tremor/react` (grandfathered). The technique
document says new components should use Ant Design, but the usage page is
already entirely Tremor. Mixing chart libraries within a single page would look
inconsistent. Two options:

**Option A (recommended): Use Tremor for visual consistency within the Usage
page.** Add an `eslint-suppressions.json` entry for the new file. This matches
the existing usage page's chart patterns and shared components
(`CustomTooltip`, `CustomLegend`, `ChartLoader`).

**Option B: Use Ant Design `@ant-design/plots`.** This follows the "modern
pattern" rule but introduces a visual discontinuity within the same page. Not
recommended for a tab inside the existing Usage page.

Choose Option A. The "modern pattern" rule applies to new standalone pages,
not to a tab inside an existing Tremor-based page where visual consistency
matters more.

---

## Part 5: How to make the renders performant and fast

This is the third question: how to prevent the dashboard from lagging. The
technique document's Step 6 says to verify performance, but performance must be
designed in from the start.

### 5.1 Data volume analysis

First, understand the data volumes:

| Metric | Points per model | Points per response | Bytes |
|--------|-----------------|---------------------|-------|
| 5m window, 15s step | 20 | 20 * 3 series * ~10 models = 600 | ~30KB |
| 15m window, 15s step | 60 | 60 * 3 * 10 = 1800 | ~90KB |
| 1h window, 30s step | 120 | 120 * 3 * 10 = 3600 | ~180KB |
| 24h window, 5m step | 288 | 288 * 3 * 10 = 8640 | ~430KB |
| 1w window, 1h step | 168 | 168 * 3 * 10 = 5040 | ~250KB |

These are small payloads. The performance risk is not payload size; it is
**render frequency** and **re-render cascades**.

### 5.2 Backend query performance

#### 5.2.1 DB queries

The SQL query scans `LiteLLM_SpendLogs` filtered by `startTime`. The
`@@index([startTime])` index makes this efficient. For a 1h window with
moderate traffic (1000 requests/hour), the query scans ~1000 rows and groups
them. This completes in under 100ms on Postgres.

For a 1w window with heavy traffic (1M requests/week), the query scans ~1M
rows. This may take 1-2 seconds. Mitigations:

1. **Use the `model_group` filter** when a specific model is selected. This
   reduces the scan if there's an index on `model_group` (there isn't
   currently; consider adding `@@index([model_group, startTime])` if this
   becomes a bottleneck).

2. **Cache the 1w query result** for 5 minutes. A 1-week-old aggregate doesn't
   change every request. Use a simple in-memory TTL cache or Redis.

3. **Consider a materialized view** only if the 1w query exceeds 3 seconds.
   Don't pre-optimize.

#### 5.2.2 Prometheus queries

The 4 PromQL range queries in `get_per_model_metrics` each take 50-200ms
depending on Prometheus retention and series cardinality. Running them in
parallel via `asyncio.gather` keeps total latency under 300ms.

### 5.3 Frontend render performance

#### 5.3.1 Memoize all derived data

Every transformation from the API response to chart data must be wrapped in
`useMemo`:

```tsx
const chartData = useMemo(() => {
  return data?.models.flatMap(m =>
    m.time_series.concurrent_requests.map(p => ({
      model: m.model_group,
      timestamp: p.timestamp,
      value: p.value,
    }))
  ) ?? [];
}, [data]);
```

The `useMemo` dependency is the query data object. React Query returns stable
references unless the data changes, so this memo is cheap.

#### 5.3.2 Use React Query's built-in performance features

```ts
useQuery({
  // ...
  placeholderData: keepPreviousData,  // prevents flash on refetch
  refetchInterval: window === "5m" ? 30000 : false,
  refetchIntervalInBackground: false,  // don't poll when tab is hidden
});
```

`keepPreviousData` (or `placeholderData: keepPreviousData` in v5) prevents the
chart from flashing to empty and back on every refetch. This is the single most
effective UX optimization for live-updating charts.

#### 5.3.3 Stagger the charts

If three charts re-render simultaneously on every poll, the browser may drop
frames. Two approaches:

1. **Render each chart in its own `React.memo` component** with the chart data
   as a prop. React will skip re-rendering a chart whose data hasn't changed
   (React Query's structural sharing means unchanged series get the same
   reference).

2. **Use `useDeferredValue` for the chart data** on the 5m/15m windows. This
   lets React prioritize user interactions (clicks, scrolls) over chart
   re-renders:

   ```tsx
   const deferredChartData = useDeferredValue(chartData);
   ```

#### 5.3.4 Cap the number of models shown

If there are 50 model groups, rendering 50 lines in a chart is unreadable and
slow. Show the top 5-10 models by throughput (configurable via a `Segmented`
control, like the existing usage page's 5/10/25/50 selector). The rest are
aggregated into an "Other" line or hidden.

#### 5.3.5 Use the existing ChartLoader during fetches

The usage page already has `src/components/shared/chart_loader.tsx`. When
`isFetching` or the window changes, show the skeleton immediately. This gives
instant visual feedback and prevents the chart from rendering stale data during
the transition.

#### 5.3.6 No client-side aggregation

The previous broken implementation fetched per-deployment data and tried to
aggregate it client-side into per-model-group. This was slow and buggy. The new
approach does all aggregation server-side. The frontend only renders
pre-aggregated time series. This keeps the client work to a minimum.

#### 5.3.7 Debounce the window selector

When the user clicks through window options rapidly (5m -> 15m -> 1h), don't
fire a request for each click. Debounce the window change by 200ms. The
existing usage page debounces the user search with 300ms; apply the same
pattern.

### 5.4 Performance budget

Set a concrete budget to verify against:

| Operation | Budget |
|-----------|--------|
| API response (5m/15m window) | < 500ms |
| API response (1h/24h/1w window) | < 1.5s |
| First contentful paint (chart skeletons) | < 100ms after click |
| Chart render (3 charts, 120 points each) | < 50ms |
| Re-render on poll (30s interval) | < 16ms (one frame) |

If any of these are exceeded, the specific bottleneck can be identified and
fixed. The design above should meet all of them for typical deployments.

---

## Part 6: Implementation order

### Phase 1: Per-request throughput (low risk, high value, small change)

1. Add "Throughput" column to `view_logs/columns.tsx` (client-side computed)
2. Add throughput to `LogDetailsDrawer/LogDetailContent.tsx`
3. Add tests for the throughput computation (edge cases: 0 tokens, 0 duration,
   null fields)
4. Run `make pre-commit`, verify in browser

This delivers immediate value with minimal risk. No backend changes.

### Phase 2: Per-model performance view (the main feature)

1. Backend: implement `GET /v1/model/performance` endpoint
   - Add sub-daily time bucketing SQL for DB-backed queries
   - Add `model_group` grouping to Prometheus queries
   - Add hybrid mode for 1h window
   - Add tests
2. Frontend: add `modelPerformanceCall` to `networking.tsx`
3. Frontend: add `useModelPerformance` hook
4. Frontend: add `ModelPerformanceView` component with 3 charts + summary table
5. Frontend: add as a new tab in the global usage view
6. Frontend: add tests (mock the API, verify rendering, verify edge cases)
7. Run `make pre-commit`, deploy, verify in browser with real data

### Phase 3: Performance hardening (only if needed)

1. Add `@@index([model_group, startTime])` migration if 1w queries are slow
2. Add 5-minute TTL cache for 24h/1w queries
3. Add `useDeferredValue` for 5m/15m chart data if frame drops occur
4. Add materialized view only if on-the-fly SQL exceeds 3s

---

## Part 7: What not to do

These are the anti-patterns that broke the previous implementation:

1. **Don't create two separate tabs for "analytics" and "real-time".** Use one
   view with automatic data source selection based on the time window.

2. **Don't group by `model_id` (deployment).** Group by `model_group`. Users
   don't know deployment IDs.

3. **Don't use daily-only SQL bucketing for a "real-time" feature.** Add
   sub-daily bucketing (`minute`/`hour`) to the SQL.

4. **Don't aggregate client-side.** Do all aggregation server-side. The
   frontend should only render pre-shaped time series.

5. **Don't pre-create aggregation tables.** Start with on-the-fly SQL. Add
   materialized views only if query latency justifies it.

6. **Don't mix Tremor and Ant Design charts in the same page.** Use Tremor
   inside the existing Usage page for visual consistency.

7. **Don't poll the DB every 15 seconds.** Only Prometheus is suited for
   sub-minute real-time. For DB-backed windows (1h+), don't poll at all, or
   poll at most every 5 minutes.

8. **Don't add the feature as a standalone page.** Add it as a tab inside the
   existing Usage page. This reuses the date picker, the view selector, the
   chart loaders, and the existing layout.

---

## Appendix A: Existing reusable components for this feature

From the technique document's Step 5 (reuse audit), these existing components
should be reused:

| Component | Location | Use for |
|-----------|----------|---------|
| `ChartLoader` | `components/shared/chart_loader.tsx` | Skeleton during fetch |
| `CustomTooltip` | `components/common_components/chartUtils.tsx` | Chart hover tooltips |
| `CustomLegend` | `components/common_components/chartUtils.tsx` | Chart legends |
| `AdvancedDatePicker` | `components/shared/advanced_date_picker.tsx` | Window selection (extend with 5m/15m) |
| `UsageViewSelect` | `UsagePage/components/UsageViewSelect/` | If adding as a new view option |
| `valueFormatter` | `UsagePage/utils/value_formatters.tsx` | Formatting large numbers |
| Tremor `AreaChart`, `LineChart`, `BarChart` | `@tremor/react` | Chart rendering |
| Tremor `Card`, `TabGroup`, `TabList`, `TabPanel` | `@tremor/react` | Layout |
| `useModels` | `hooks/models/useModels.ts` | Model list for selector |

## Appendix B: Existing API wrappers to reuse or extend

| Wrapper | Endpoint | Reuse for |
|---------|----------|-----------|
| `uiSpendLogsCall` | `/spend/logs/ui` | Per-request data (already has timing fields) |
| `modelInfoCall` | `/v2/model/info` | Model list |
| `adminGlobalActivityPerModel` | `/global/activity/model` | Daily model activity (existing) |

The new `modelPerformanceCall` is the only new wrapper needed.

## Appendix C: Data flow for the new feature

```
UsagePageView (global view, new "Model Performance" tab)
    │
    ▼
ModelPerformanceView (new component)
    │  window selector: 5m / 15m / 1h / 24h / 1w
    │  model selector: optional, from response
    │
    ▼
useModelPerformance(window, modelGroup)  (new hook, React Query)
    │  refetchInterval: 30s for 5m/15m, none for 1h+
    │  keepPreviousData: true
    │
    ▼
modelPerformanceCall(accessToken, {window, model_group})  (new wrapper)
    │
    ▼
GET /v1/model/performance?window=1h&model_group=gpt-4
    │
    ├── if window is 5m/15m:  Prometheus query_range (4 PromQL queries)
    ├── if window is 1h:       Hybrid (Prometheus + DB, parallel)
    └── if window is 24h/1w:   DB query (sub-daily bucketed SQL)
    │
    ▼
Response: { models: [{ model_group, time_series: {...}, summary: {...} }] }
    │
    ▼
useMemo → chartData arrays
    │
    ▼
3 Tremor charts (React.memo'd, ChartLoader during fetch)
```

---

## Appendix D: Live Endpoint Test Observations

> Tested 2026-07-30 against a running LiteLLM proxy (port 4000) with PostgreSQL.
> Provider API keys were intentionally empty, so all LLM calls failed at the
> upstream auth layer. This still creates spend-log rows, making it possible to
> observe endpoint response shapes and silent filtering behavior.

### Test setup

- Proxy: `litellm/proxy/proxy_cli.py --config litellm/proxy/dev_config.yaml`
- DB: PostgreSQL 15 via `DATABASE_URL=postgresql://llmproxy:...@localhost:5432/litellm`
- Schema: `prisma db push --schema=schema.prisma`
- Auth: master key `sk-1234` (Bearer token)
- Test call: `POST /v1/chat/completions` with `model: "anthropic-haiku-4-5"`
- 3 spend-log rows generated (1 initial + 2 router retries), all `status="failure"`

### D.1 `GET /model/metrics/per_model` (Prometheus-backed)

**Request:**
```
GET /model/metrics/per_model?window=1h
Authorization: Bearer sk-1234
```

**Response (200):**
```json
{
  "prometheus_connected": false,
  "window": "1h",
  "step": "",
  "deployments": []
}
```

**Observation:** With no Prometheus configured (or Prometheus unreachable), the
endpoint returns `prometheus_connected: false` and an empty `deployments` array.
It does NOT error. The frontend must handle this graceful degradation.

### D.2 `GET /model/metrics` (DB-backed, daily granularity)

**Request:**
```
GET /model/metrics?model_group=anthropic-haiku-4-5&start_date=2026-07-29&end_date=2026-07-31
Authorization: Bearer sk-1234
```

**Response (200):**
```json
{
  "data": [],
  "all_api_bases": []
}
```

**Observation:** Returns empty despite 3 spend-log rows existing in the date
range. Root cause: the SQL contains `HAVING SUM(completion_tokens) > 0`, which
silently excludes all failed requests (failed calls have 0 completion tokens).
**This is a silent data-loss trap** — if all requests to a model fail, the model
appears to have zero traffic in this endpoint, even though spend logs exist.

### D.3 `GET /model/streaming_metrics` (DB-backed, TTFT)

**Request:**
```
GET /model/streaming_metrics?model_group=anthropic-haiku-4-5&start_date=2026-07-29&end_date=2026-07-31
Authorization: Bearer sk-1234
```

**Response (200):**
```json
{
  "data": [],
  "all_api_bases": []
}
```

**Observation:** Returns empty for the same reason as D.2 but with a different
filter: the SQL contains `WHERE "completionStartTime" != "endTime"`. For failed
calls, all three timestamps (`startTime`, `completionStartTime`, `endTime`) are
identical, so the `!=` filter excludes them. Same silent data-loss trap.

### D.4 `GET /model/metrics/slow_responses` (DB-backed)

**Request:**
```
GET /model/metrics/slow_responses?model_group=anthropic-haiku-4-5&threshold=0
Authorization: Bearer sk-1234
```

**Response (200):**
```json
[]
```

**Observation:** Returns a flat JSON array (not an object wrapper). The SQL
groups by `api_base` only — `model_group` is a WHERE filter, not a GROUP BY
column. For failed calls, `api_base` is an empty string, so the row appears as
`{api_base: "", total_count: 3, slow_count: 0}` (with `threshold=0`, all calls
are "slow" by definition, but the failed calls have 0 duration so they don't
exceed any positive threshold). With `threshold=0` the response was `[]` because
the SQL likely has `WHERE EXTRACT(epoch FROM ("endTime" - "startTime")) > $2`
and 0 > 0 is false.

### D.5 `GET /spend/logs/ui` (DB-backed, per-request)

**Request:**
```
GET /spend/logs/ui?start_date=2026-07-29%2000:00:00&end_date=2026-07-31%2023:59:59
Authorization: Bearer sk-1234
```

**Response (200, abbreviated):**
```json
{
  "data": [
    {
      "request_id": "d3a2...",
      "call_type": "completion",
      "api_key": "sk-1234",
      "spend": 0.0,
      "total_tokens": 0,
      "prompt_tokens": 0,
      "completion_tokens": 0,
      "startTime": "2026-07-30T13:45:01.234Z",
      "endTime": "2026-07-30T13:45:01.234Z",
      "completionStartTime": "2026-07-30T13:45:01.234Z",
      "model": "anthropic-haiku-4-5",
      "model_id": "0a4ae6fe...",
      "model_group": "anthropic-haiku-4-5",
      "custom_llm_provider": "anthropic",
      "api_base": "",
      "user": "",
      "metadata": {
        "status": "failure",
        "error_information": { "error": "401 - {...}" }
      },
      "cache_hit": "None",
      "cache_key": null,
      "request_tags": [],
      "team_id": null,
      "organization_id": null,
      "end_user": null,
      "requester_ip_address": "127.0.0.1",
      "session_id": null,
      "status": "failure",
      "request_duration_ms": 0,
      "session_total_count": "None"
    }
    // ... 2 more entries (retries with empty model_id)
  ],
  "user_api_keys": [],
  // ... other filter metadata arrays
}
```

**Key observations:**
1. **Date format:** Requires `YYYY-MM-DD HH:MM:SS` (with URL-encoded space as
   `%20`). Passing `YYYY-MM-DD` only returns 400. This differs from D.6.
2. **Failed-request shape:** All timestamps are identical (`startTime` =
   `endTime` = `completionStartTime`), `request_duration_ms` is 0, `api_base` is
   empty string, tokens are all 0.
3. **`model_id` on retries:** The first attempt has a populated `model_id`, but
   router-retry attempts have an empty `model_id`. This is a data-quality issue
   for any analytics that group by `model_id`.
4. **`api_base` empty for failures:** The upstream URL was never reached (auth
   failed before the HTTP call completed), so `api_base` is `""`. Any endpoint
   that groups by `api_base` will bucket all failures under `""`.
5. **Rich response:** This endpoint returns the most complete per-request data
   of all endpoints — it is the best source for per-request observability.

### D.6 `GET /global/activity/model` (DB-backed, daily aggregation)

**Request:**
```
GET /global/activity/model?start_date=2026-07-29&end_date=2026-07-31
Authorization: Bearer sk-1234
```

**Response (200):**
```json
[
  {
    "model": "anthropic-haiku-4-5",
    "daily_data": [
      {
        "model_group": "anthropic-haiku-4-5",
        "date": "Jul 30",
        "api_requests": 3,
        "total_tokens": 0
      }
    ],
    "sum_api_requests": 3,
    "sum_total_tokens": 0
  }
]
```

**Key observations:**
1. **Date format:** Requires `YYYY-MM-DD` (date only, no time). Passing
   `YYYY-MM-DD HH:MM:SS` causes a 500 Internal Server Error because
   `datetime.strptime(start_date, "%Y-%m-%d")` throws `ValueError`. This is the
   opposite of D.5 and is a **footgun** for the frontend.
2. **No failure filtering:** Unlike D.2/D.3, this endpoint does NOT filter out
   failed requests — it counts all 3 failed calls as `api_requests: 3` with
   `total_tokens: 0`.
3. **Date format in response:** `date` is formatted as `"Jul 30"` (month
   abbreviation + day), not ISO format. The frontend must parse this.
4. **Response shape:** Returns a flat array (not wrapped in an object).

### D.7 `GET /metrics/` (Prometheus scrape endpoint)

**Request:**
```
GET /metrics/  (trailing slash required — /metrics redirects with 307)
Authorization: Bearer sk-1234
```

**Key observations:**
1. **URL:** The endpoint is `/metrics/` (with trailing slash). A request to
   `/metrics` (no slash) returns `307 Temporary Redirect` to `/metrics/`. The
   Prometheus ServiceMonitor must be configured with the trailing slash.
2. **`model_group` label absence:** Confirmed live — **none** of the 7
   `litellm_*` metrics carry a `model_group` label. The labels present are:
   - `litellm_deployment_state`: `{api_base, api_provider, litellm_model_name, model_id}`
   - `litellm_deployment_in_progress_requests`: same labels
   - `litellm_deployment_cooled_down_total/created`: same + `{exception_status}`

   The 4 metrics that DO have `model_group` (`litellm_overhead_latency_metric`,
   `litellm_overhead_with_guardrails_latency_metric`, and 2 others) are defined
   as histograms but did not emit any data rows in this test (failed call never
   reached the post-call callback). Their HELP/TYPE lines are present, confirming
   they are registered, but no bucket samples were emitted.

3. **Stuck `litellm_deployment_in_progress_requests`:** After the failed call,
   this metric remained at `1.0` instead of returning to `0.0`. This appears to
   be a bug where the decrement callback doesn't fire on upstream auth failures.

### D.8 Summary of date-format requirements

| Endpoint | Required format | Error if wrong format |
|---|---|---|
| `/spend/logs/ui` | `YYYY-MM-DD HH:MM:SS` | 400 Bad Request |
| `/global/activity/model` | `YYYY-MM-DD` | 500 Internal Server Error |
| `/model/metrics` | `YYYY-MM-DD` (FastAPI auto-parses) | — |
| `/model/streaming_metrics` | `YYYY-MM-DD` (FastAPI auto-parses) | — |
| `/model/metrics/slow_responses` | n/a (no date params) | — |
| `/model/metrics/per_model` | n/a (uses `window` param) | — |

**Frontend implication:** The `modelPerformanceCall` wrapper must format dates
differently depending on which underlying endpoint it calls. A single
`formatDate` utility is insufficient.

### D.9 Summary of silent-failure-filtering behavior

| Endpoint | SQL filter that excludes failures | Effect |
|---|---|---|
| `/model/metrics` | `HAVING SUM(completion_tokens) > 0` | Models with only failed calls appear to have 0 traffic |
| `/model/streaming_metrics` | `WHERE "completionStartTime" != "endTime"` | Models with only failed calls have no TTFT data |
| `/model/metrics/slow_responses` | `WHERE EXTRACT(epoch FROM ("endTime" - "startTime")) > threshold` | Failed calls (0 duration) never count as "slow" |
| `/spend/logs/ui` | (none) | Failed calls ARE included — most reliable for observability |
| `/global/activity/model` | (none) | Failed calls ARE counted as api_requests |

**Implementation implication:** The per-model observability feature must use
`/spend/logs/ui` as the primary data source for the DB-backed path, not
`/model/metrics` or `/model/streaming_metrics`, because the latter silently
exclude failures. The `/model/metrics` and `/model/streaming_metrics` endpoints
are only suitable for successful-call analytics (latency, throughput on
successful requests).

---

## Appendix E: Per-Deployment Observability — Discovery & Design

Date: 2026-07-30.

### E.1 What is a "deployment" in LiteLLM?

A **model group** (a.k.a. `model_name`) is what callers request, e.g.
`anthropic-haiku-4-5`. Behind each model group, the router holds a list of
**deployments** — each deployment is one upstream configuration (one
`api_base` + `api_key` + `model` triplet). Multiple deployments sharing the
same `model_name` form a load-balanced pool with retries, fallbacks, and
cooldowns.

Every deployment gets a **`model_id`** — a deterministic hash generated by
`router._generate_model_id()` (`router.py:7314`) from the
`litellm_params` + `model_info` dict. This `model_id` is:

- Stored in `router.model_list[i]["model_info"]["id"]`
- Indexed in `router.model_id_to_deployment_index_map` for O(1) lookup
  (`router.py:476`)
- Looked up via `router.get_deployment(model_id)` (`router.py:8337`)
- Listed via `router.get_model_ids(model_name)` (`router.py:9124`)

**Concrete example:** A model group `claude-haiku` could have 3 deployments:

| model_id (truncated) | api_base | api_provider | model |
|---|---|---|---|
| `0a4ae6fe...` | (default Anthropic URL) | anthropic | `anthropic/claude-haiku-4-5` |
| `4eabc624...` | `https://my-azure.azure.com/...` | azure_ai | `azure_ai/claude-haiku-4-5` |
| `2a46d15a...` | (AWS Bedrock) | bedrock | `bedrock/claude-haiku-4-5` |

All three share `model_name = claude-haiku` but differ in upstream. When one
fails, the router cools it down (by `model_id`) and retries on another.

> **Live observation (dev_config.yaml):** All 39 model groups in the current
> dev config are single-deployment (one entry per `model_name`). The
> multi-deployment scenario is exercised by production configs where the same
> `model_name` is listed multiple times with different `litellm_params`.

### E.2 Prometheus per-deployment metrics (already available)

LiteLLM's Prometheus integration (`litellm/integrations/prometheus.py`) defines
**12 deployment-level metrics**, all carrying 4 canonical labels:

| Label | Meaning |
|---|---|
| `litellm_model_name` | The model group name (what the caller requested) |
| `model_id` | The deployment hash (unique per upstream config) |
| `api_base` | The upstream base URL |
| `api_provider` | The provider type (anthropic, azure_ai, bedrock, etc.) |

The 12 metrics:

| Metric | Type | Extra labels | What it measures | Source line |
|---|---|---|---|---|
| `litellm_deployment_state` | Gauge | — | 0=healthy, 1=partial, 2=complete outage | `prometheus.py:440` |
| `litellm_deployment_in_progress_requests` | Gauge (livesum) | — | Concurrent in-flight requests per deployment | `prometheus.py:455` |
| `litellm_deployment_cooled_down` | Counter | `exception_status` | Count of cooldown events per deployment | `prometheus.py:470` |
| `litellm_deployment_success_responses` | Counter | — | Successful responses per deployment | `prometheus.py:483` |
| `litellm_deployment_failure_responses` | Counter | `exception_status`, `exception_class` | Failed responses per deployment | `prometheus.py:493` |
| `litellm_deployment_total_requests` | Counter | — | Total requests (success + failure) per deployment | `prometheus.py:503` |
| `litellm_deployment_latency_per_output_token` | Histogram | — | Latency per output token (p50/p90/p99) | `prometheus.py:515` |
| `litellm_deployment_tpm_limit` | Gauge | — | TPM limit configured for the deployment | `prometheus.py:525` |
| `litellm_deployment_rpm_limit` | Gauge | — | RPM limit configured for the deployment | `prometheus.py:530` |
| `litellm_deployment_successful_fallbacks` | Counter | `requested_model`, `fallback_model`, `api_key_hash`, `api_key_alias`, `team`, `team_alias`, `exception_status`, `exception_class` | Successful fallbacks FROM this deployment | `prometheus.py:535` |
| `litellm_deployment_failed_fallbacks` | Counter | (same as above) | Failed fallbacks FROM this deployment | `prometheus.py:545` |

> **Label definitions** are in `litellm/types/integrations/prometheus.py`
> (lines 490–560). The 4 canonical labels are reused across all 12 metrics
> except the fallback metrics, which use a richer label set including
> `requested_model`/`fallback_model` for fallback chain tracking.

**Instrumentation lifecycle:**

- `litellm_deployment_in_progress_requests` is **incremented** at the pre-call
  hook (`prometheus.py:1246`) and **decremented** on both success
  (`prometheus.py:2738`) and failure (`prometheus.py:2491`).
- `litellm_deployment_state` is set to `2` (complete outage) when a deployment
  is cooled down (`cooldown_callbacks.py:38–62`) and reset to `0` when it
  succeeds again (`prometheus.py:3111`).
- All other counters are incremented in the post-call success/failure
  callback paths.

### E.3 Existing endpoint: `/model/metrics/per_model`

**File:** `litellm/proxy/model_metrics_endpoints/per_model_endpoints.py` (85 lines)

**Parameters:** `window` (1m/15m/1h/24h/7d), optional `model_id` filter

**How it works:**
1. Checks `is_prometheus_connected()` — returns `PROMETHEUS_URL is not None`
2. If not connected: falls back to `get_in_progress_requests_instant()` (instant
   gauge only, no historical data)
3. If connected: runs 4 PromQL range queries grouped by `model_id`:

| Series | PromQL |
|---|---|
| `concurrent_requests` | `max by (model_id) (max_over_time(litellm_deployment_in_progress_requests[window]))` |
| `request_rate` | `sum by (model_id) (rate(litellm_deployment_total_requests_total[window]))` |
| `output_tokens_per_sec` | `sum by (model_id) (rate(litellm_output_tokens_metric_total[window]))` |
| `latency_per_token_p50` | `histogram_quantile(0.50, sum by (le, model_id) (rate(litellm_deployment_latency_per_output_token_bucket[window])))` |

4. Recovers `litellm_model_name`, `api_base`, `api_provider`, `rpm_limit` via
   a secondary instant query on label-carrying gauges
   (`_get_deployment_label_metadata()`, `prometheus_api.py:251`)
5. Returns `{prometheus_connected, window, step, deployments: [...]}`

**Response shape per deployment:**
```json
{
  "model_id": "0a4ae6feafa1e519134e1706c",
  "litellm_model_name": "anthropic-haiku-4-5",
  "api_base": "",
  "api_provider": "anthropic",
  "rpm_limit": 0,
  "concurrent_requests": [[ts, "0"], ...],
  "request_rate": [[ts, "0.5"], ...],
  "output_tokens_per_sec": [[ts, "12.3"], ...],
  "latency_per_token_p50": [[ts, "0.08"], ...]
}
```

> **Live observation:** Without `PROMETHEUS_URL` set in the environment, the
> endpoint returns `{"prometheus_connected": false, "deployments": []}`. The
> `PROMETHEUS_URL` env var must point to a running Prometheus instance that is
> scraping the proxy's `/metrics/` endpoint.

### E.4 DB-backed per-deployment data (available but not exposed)

Both `LiteLLM_SpendLogs` and `LiteLLM_ErrorLogs` tables contain `model_id` and
`api_base` columns, enabling per-deployment aggregation:

**`LiteLLM_SpendLogs` columns available for per-deployment aggregation:**

| Column | Type | Use |
|---|---|---|
| `model_id` | String | Deployment identifier (group-by key) |
| `api_base` | String | Upstream URL (secondary group-by) |
| `model` | String | The actual model string sent upstream |
| `model_group` | String | The model group name |
| `custom_llm_provider` | String | Provider type |
| `spend` | Float | Cost in USD |
| `total_tokens` | Int | Total tokens used |
| `prompt_tokens` | Int | Input tokens |
| `completion_tokens` | Int | Output tokens |
| `startTime` | DateTime | Request start |
| `endTime` | DateTime | Request end |
| `request_duration_ms` | Int | Total duration |
| `completionStartTime` | DateTime | First token time (streaming) |
| `status` | String | success/failure |

**`LiteLLM_ErrorLogs` columns available:**

| Column | Type | Use |
|---|---|---|
| `model_id` | String | Deployment identifier |
| `api_base` | String | Upstream URL |
| `model_group` | String | Model group |
| `litellm_model_name` | String | LiteLLM model name |
| `exception_type` | String | Exception class |
| `exception_string` | String | Full exception message |
| `status_code` | String | HTTP status code |
| `request_kwargs` | JSON | Request payload (for debugging) |

> **Live observation (DB query):** The `model_id` column IS populated in
> `LiteLLM_SpendLogs` for the **first attempt** of a request, but is **NULL**
> for router-retry attempts (the retry path doesn't propagate `model_id` into
> the spend log). This is a data-quality gap: per-deployment DB aggregation
> will undercount retry traffic. Example from live DB:
> ```
> model_id=0a4ae6feafa1e519134e  model=anthropic/claude-haiku-4-5  status=failure  (first attempt)
> model_id=NULL                  model=anthropic-haiku-4-5         status=failure  (retry attempt)
> ```
> Only 1 distinct `model_id` was found in spend logs: `0a4ae6feafa1e519134e1706c`
> (group `anthropic-haiku-4-5`, count=2).

### E.5 Internal router state (available but not exposed via HTTP)

Three internal data structures track per-deployment runtime state but are not
exposed via any HTTP endpoint:

#### E.5.1 Cooldown cache

**File:** `litellm/router_utils/cooldown_cache.py`

```python
class CooldownCacheValue(TypedDict):
    exception_received: str    # masked exception message
    status_code: str           # HTTP status code that triggered cooldown
    timestamp: float           # epoch when cooldown started
    cooldown_time: float       # total cooldown duration in seconds
```

- Cache key: `"deployment:" + model_id + ":cooldown"`
- `async_get_active_cooldowns(model_ids)` at line 112 returns
  `List[Tuple[model_id, CooldownCacheValue]]`
- `_async_get_cooldown_deployments_with_debug_info()` at
  `cooldown_handlers.py:325` returns full metadata

**Available per cooled-down deployment:** which deployment (`model_id`), why
(`exception_received`, `status_code`), when (`timestamp`), how long
(`cooldown_time`), remaining time (`timestamp + cooldown_time - now()`).

#### E.5.2 Health state cache

**File:** `litellm/router_utils/health_state_cache.py`

```python
class DeploymentHealthStateValue(TypedDict):
    is_healthy: bool
    timestamp: float    # epoch of last health check
    reason: str         # human-readable reason
```

- Stored under cache key `litellm:health_check:deployment_health_state`
- Populated by background health checks via `build_deployment_health_states()`
  (`proxy_server.py:3131`)
- `async_get_unhealthy_deployment_ids()` returns currently-unhealthy deployments

#### E.5.3 Per-minute success/fail tracking

**File:** `litellm/router_utils/track_deployment_metrics.py`

- Tracks per-minute success/failure counts per `deployment_id`
- In-memory cache, TTL 60 seconds
- Used internally for cooldown decisions (percent_fails calculation)
- Not exposed via HTTP

### E.6 Gap analysis: what's missing for full per-deployment observability

| # | Gap | Impact |
|---|---|---|
| 1 | **No unified per-deployment endpoint** combining DB + Prometheus data | Must call 3+ endpoints and join client-side to get a complete picture |
| 2 | **DB spend/errors not aggregated by `model_id`** in any endpoint | `/model/metrics/per_model` only queries Prometheus; `/spend/logs/ui` returns raw rows, not per-deployment aggregates |
| 3 | **Cooldown metadata not exposed via HTTP** | Can't see which deployments are currently cooled down, why, or when they'll recover |
| 4 | **Health check state not exposed via HTTP** | Can't see which deployments are marked unhealthy by background checks |
| 5 | **`/model/metrics/per_model` only covers 4 time-series** | Missing: spend, error rate, success rate, cooldown status, token counts, TTFT, total duration — all available in DB but not in this endpoint |

### E.7 Design recommendation: `/v1/deployment/metrics` endpoint

A new endpoint should aggregate per-deployment data from three sources:

```
GET /v1/deployment/metrics?model_group=claude-haiku&window=1h
```

**Response shape (per deployment):**
```json
{
  "deployments": [
    {
      "model_id": "0a4ae6feafa1e519134e1706c",
      "litellm_model_name": "claude-haiku",
      "api_base": "https://api.anthropic.com",
      "api_provider": "anthropic",

      "state": "healthy",
      "is_cooled_down": false,
      "cooldown_reason": null,
      "cooldown_remaining_seconds": null,
      "health_check": {"is_healthy": true, "reason": "", "last_checked": "2026-07-30T18:00:00Z"},

      "prometheus": {
        "concurrent_requests": 0,
        "request_rate": 5.2,
        "output_tokens_per_sec": 12.3,
        "latency_per_token_p50": 0.08,
        "rpm_limit": 60,
        "tpm_limit": 100000
      },

      "database": {
        "total_requests": 1200,
        "successful_requests": 1150,
        "failed_requests": 50,
        "success_rate": 0.958,
        "error_rate": 0.042,
        "total_spend": 1.23,
        "total_tokens": 450000,
        "prompt_tokens": 300000,
        "completion_tokens": 150000,
        "avg_duration_ms": 850,
        "avg_ttft_ms": 120,
        "top_errors": [
          {"exception_type": "RateLimitError", "status_code": "429", "count": 30},
          {"exception_type": "Timeout", "status_code": "408", "count": 20}
        ]
      }
    }
  ]
}
```

**Data flow:**

1. **Router** → enumerate deployments for the requested `model_group` via
   `router.get_model_ids(model_name)` — provides the list of `model_id`s and
   their `litellm_params` (api_base, api_provider, model)
2. **Cooldown cache** → `async_get_active_cooldowns(model_ids)` — provides
   cooldown state + metadata
3. **Health state cache** → `async_get_unhealthy_deployment_ids()` — provides
   health check state
4. **Prometheus** → `get_per_model_metrics(window, model_id)` per deployment —
   provides real-time concurrent/rate/latency (requires `PROMETHEUS_URL`)
5. **Database** → aggregate `LiteLLM_SpendLogs` GROUP BY `model_id` — provides
   historical spend/tokens/duration/success/failure counts
6. **Database** → aggregate `LiteLLM_ErrorLogs` GROUP BY `model_id`,
   `exception_type` — provides error breakdown

**Key design decisions:**

- **DB aggregation query:** `SELECT model_id, COUNT(*) FILTER (WHERE status='success') as success, COUNT(*) FILTER (WHERE status!='success') as failures, SUM(spend), SUM(total_tokens), AVG(request_duration_ms), ... FROM "LiteLLM_SpendLogs" WHERE model_id = $1 AND "startTime" >= $2 GROUP BY model_id`
- **Graceful degradation:** If Prometheus is not connected, return
  `prometheus: null` but still return DB + cooldown + health data. If DB is
  empty, return `database: null` but still return Prometheus + cooldown data.
- **`model_id` NULL gap:** The retry-path NULL `model_id` issue (E.4) means DB
  counts will undercount. This should be tracked as a known limitation and
  fixed separately in the router logging path.

### E.8 Summary: available per-deployment data today

| Data source | Granularity | Access method | Exposed via HTTP? |
|---|---|---|---|
| Concurrent in-flight requests | Per `model_id` | Prometheus gauge | ✅ `/model/metrics/per_model` |
| Request rate | Per `model_id` | Prometheus counter | ✅ `/model/metrics/per_model` |
| Output tokens/sec | Per `model_id` | Prometheus counter | ✅ `/model/metrics/per_model` |
| Latency per token p50 | Per `model_id` | Prometheus histogram | ✅ `/model/metrics/per_model` |
| Success/failure/total counts | Per `model_id` + labels | Prometheus counters | ❌ (scrape `/metrics/` only) |
| Deployment state (healthy/outage) | Per `model_id` | Prometheus gauge | ❌ (scrape `/metrics/` only) |
| Cooldown events | Per `model_id` + status | Prometheus counter | ❌ (scrape `/metrics/` only) |
| TPM/RPM limits | Per `model_id` | Prometheus gauge | ❌ (scrape `/metrics/` only) |
| Spend, tokens, duration | Per `model_id` in DB | `LiteLLM_SpendLogs` | ❌ (no endpoint aggregates by model_id) |
| Errors with exceptions | Per `model_id` in DB | `LiteLLM_ErrorLogs` | ❌ (no endpoint) |
| Cooldown details (reason, remaining) | Per `model_id` | Router `cooldown_cache` | ❌ (internal only) |
| Health check results | Per `model_id` | Router `health_state_cache` | ❌ (internal only) |
| Per-minute success/fail counts | Per `model_id` | Router in-memory cache | ❌ (internal only) |

**Bottom line:** LiteLLM already captures rich per-deployment data — the
`model_id` key exists in Prometheus metrics, DB tables, and internal router
caches. The gap is purely in **exposure**: no single endpoint aggregates and
returns per-deployment observability. The `/v1/deployment/metrics` endpoint
design (E.7) bridges this gap by joining all three data sources.
