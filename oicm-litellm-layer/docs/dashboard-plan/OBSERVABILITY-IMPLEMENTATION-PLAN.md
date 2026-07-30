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
(`litellm/litellm_core_utils/litellm_logging.py`), which tracks these
timestamps:

| Timestamp | Set when | Used for |
|-----------|----------|----------|
| `start_time` | Logging init (request begins) | Total time start |
| `api_call_start_time` | Just before the provider HTTP call | TTFT start |
| `completion_start_time` | First streamed chunk (streaming only) | TTFT end |
| `end_time` | Response complete | Total time end |

From these, three derived metrics are computed:

| Metric | Formula | Where it's stored |
|--------|---------|-------------------|
| **Total request time** | `end_time - start_time` | `LiteLLM_SpendLogs.request_duration_ms` (DB column, int ms) |
| **TTFT** (streaming only) | `completion_start_time - api_call_start_time` | `LiteLLM_SpendLogs.completionStartTime` (DB column, DateTime; TTFT is computed on read) |
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
| `litellm_output_tokens_metric` | Counter | Output tokens per deployment |
| `litellm_deployment_latency_per_output_token` | Histogram | Latency / completion_tokens per deployment |

### 1.3 Backend: existing endpoints

| Endpoint | Source | What it returns | Time resolution |
|----------|--------|-----------------|----------------|
| `GET /model/metrics/per_model` | Prometheus | concurrent, request_rate, output_tokens_per_sec, latency_per_token_p50 | 15s/30s/5m/1h steps |
| `GET /model/metrics` | DB | avg latency_per_token per model per day | Daily |
| `GET /model/streaming_metrics` | DB | TTFT per model (per-request or daily avg) | Per-request or daily |
| `GET /model/metrics/slow_responses` | DB | count of slow requests per model_group | Aggregated |
| `GET /spend/logs/ui` | DB | Per-request logs with `request_duration_ms`, `completionStartTime` | Per-request |
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
   `spend_management_endpoints.py`. The sort would compute
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

2. **For Prometheus queries**, change the PromQL to group by `model_group`
   label instead of `model_id`. The `litellm_*` metrics already carry a
   `model_group` label (set in `prometheus.py` via `_get_labels`). The current
   `get_per_model_metrics` groups by `model_id` only; the new endpoint would
   use `sum by (model_group) (rate(...))` instead.

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
