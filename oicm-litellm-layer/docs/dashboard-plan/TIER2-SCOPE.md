# Tier 2 Scope: Per-Model Real-Time and Historical Analytics

Status: implementing.
Date: 2026-07-22.

## What the user wants

A per-registered-model analytics view with:

1. Concurrent requests over time
2. Throughput (tokens per second) over time
3. Request rate (requests per second) over time
4. Time-window selector: 1m, 15m, 1h, 24h, 7d

All scoped per model (per deployment, identified by model_id + api_base).

## What already exists after Tier 0

52 litellm metrics are flowing into Prometheus. The metrics needed for throughput and request rate already exist; only concurrent requests is missing.

### Existing metrics that cover throughput and request rate

| Metric | Type | Labels (litellm-scoped) | Derives |
|--------|------|------------------------|---------|
| `litellm_deployment_total_requests_total` | Counter | `model_id`, `api_base`, `litellm_model_name`, `api_provider`, `requested_model`, `api_key_alias`, `hashed_api_key`, `team`, `team_alias`, `client_ip`, `user_agent` | `rate(...[window])` = requests/sec per deployment |
| `litellm_output_tokens_metric_total` | Counter | `model`, `model_id`, `api_provider`, `api_key_alias`, `hashed_api_key`, `team`, `team_alias`, `end_user`, `user`, `user_email`, `org_id`, `org_alias`, `requested_model` | `rate(...[window])` = output tokens/sec per model |
| `litellm_input_tokens_metric_total` | Counter | (same as output_tokens) | `rate(...[window])` = input tokens/sec per model |
| `litellm_total_tokens_metric_total` | Counter | (same as output_tokens) | `rate(...[window])` = total tokens/sec per model |
| `litellm_deployment_latency_per_output_token_bucket` | Histogram | `model_id`, `api_base`, `litellm_model_name`, `api_provider`, `api_key_alias`, `hashed_api_key`, `team`, `team_alias`, `le` | `histogram_quantile(0.5, rate(...[window]))` = median seconds/token, inverse = tokens/sec |
| `litellm_llm_api_latency_metric_bucket` | Histogram | `model`, `model_id`, `api_provider`, `api_key_alias`, `hashed_api_key`, `team`, `team_alias`, `end_user`, `le` | `histogram_quantile(0.95, rate(...[window]))` = p95 latency |
| `litellm_llm_api_time_to_first_token_metric_bucket` | Histogram | (same as llm_api_latency) | `histogram_quantile(0.95, rate(...[window]))` = p95 TTFT |
| `litellm_deployment_rpm_limit` | Gauge | `model_id`, `api_base`, `litellm_model_name`, `api_provider` | RPM limit for headroom calculation |
| `litellm_deployment_state` | Gauge | `model_id`, `api_base`, `litellm_model_name`, `api_provider` | healthy=1 / unhealthy=0 |
| `litellm_in_flight_requests` | Gauge | (proxy-level, no model labels) | All HTTP requests in flight at proxy level |

### What is missing

`litellm_deployment_in_progress_requests` — a Gauge labeled with `model_id`, `api_base`, `litellm_model_name`, `api_provider` that tracks how many LLM calls to a specific deployment are currently in flight (started but not yet completed). This is different from `litellm_in_flight_requests` (which tracks all HTTP requests to the proxy, including /health, /v1/models, management endpoints, etc., and has no per-deployment labels).

## Architecture decision: query Prometheus directly

The existing model metrics endpoints (`/model/metrics/slow_responses`, etc.) query PostgreSQL (`LiteLLM_SpendLogs`). Tier 2 takes a different approach: query Prometheus directly via the Prometheus HTTP API.

Reasons:
- Throughput (tokens/sec) and request rate (req/sec) are computed via `rate()` over a time window. Postgres doesn't have this; Prometheus does natively.
- Time-series charts need `query_range` (multiple data points over time). Postgres endpoints return aggregated rows, not time series.
- The metrics already exist in Prometheus. No new data pipeline needed.
- The `PROMETHEUS_URL` env var and `prometheus_api.py` helper module already exist.

Fallback: when `PROMETHEUS_URL` is not set, the concurrent-requests endpoint returns the in-memory gauge value (instant only, no time series). Throughput and request rate cannot be computed without Prometheus, so those cards show a "Prometheus not connected" state.

## Architecture: reuse existing patterns, minimal new code

litellm already has an extracted-router pattern (74 `include_router` calls in `proxy_server.py`). Each endpoint group directory (`analytics_endpoints/`, `spend_tracking/`, etc.) is a self-contained vertical slice. Tier 2 follows this pattern, not a formal VSA framework.

### Key reuse opportunities found

1. **`oicm-litellm-layer/hooks/keda_metrics.py`** already implements a concurrent-requests gauge using the `CustomLogger` hook lifecycle: `async_log_pre_api_call` (inc) + `async_log_success_event`/`async_log_failure_event` (dec). This proves the inc/dec contract works in production. Tier 2 integrates this into `PrometheusLogger` for consistency with the existing metric factory system.

2. **`litellm/integrations/prometheus_helpers/prometheus_api.py`** is the existing extension point for Prometheus HTTP API queries. The `get_daily_spend_from_prometheus` function shows the `query_range` pattern. Tier 2 adds 2 functions here.

3. **`litellm/proxy/spend_tracking/spend_management_endpoints.py:2686`** shows the Prometheus-vs-fallback pattern: `if is_prometheus_connected(): query prometheus else: query postgres`. Tier 2 copies this gate.

4. **`litellm/proxy/analytics_endpoints/analytics_endpoints.py`** shows the extracted router pattern. Tier 2 creates a new directory following this template.

5. **`PrometheusLogger.set_llm_deployment_success_metrics` / `set_llm_deployment_failure_metrics`** already have all 4 deployment labels and fire per-LLM-call-attempt. Adding `gauge.dec()` here is 1 line each.

## Implementation plan

### Backend

#### 1. New Prometheus gauge: `litellm_deployment_in_progress_requests`

File: `litellm/integrations/prometheus.py`

Add to `PrometheusLogger.__init__`, using `multiprocess_mode="livesum"` (correct for in-flight counts across workers, matching the existing `litellm_in_flight_requests` gauge):

```python
self.litellm_deployment_in_progress_requests = self._gauge_factory(
    name="litellm_deployment_in_progress_requests",
    documentation="Number of LLM API calls currently in progress per deployment",
    labelnames=self.get_labels_for_metric("litellm_deployment_in_progress_requests"),
    multiprocess_mode="livesum",
)
```

File: `litellm/types/integrations/prometheus.py`

```python
litellm_deployment_in_progress_requests = [
    UserAPIKeyLabelNames.v2_LITELLM_MODEL_NAME.value,
    UserAPIKeyLabelNames.MODEL_ID.value,
    UserAPIKeyLabelNames.API_BASE.value,
    UserAPIKeyLabelNames.API_PROVIDER.value,
]
```

Same 4-label set as `litellm_deployment_state` and `litellm_deployment_rpm_limit`.

#### 2. Inc/dec hooks in PrometheusLogger (not in router)

The inc/dec uses the existing `CustomLogger` hook lifecycle, proven by `keda_metrics.py`:

- **Inc**: Add `async_log_pre_api_call` to `PrometheusLogger` (it does not have one). At this point, `kwargs["metadata"]["model_info"]` contains the deployment identity (set by the router before calling `litellm.acompletion`). Extract `model_id`, `api_base`, `litellm_model_name`, `api_provider` from kwargs and call `gauge.labels(...).inc()`.

- **Dec (success)**: Add 1 line to `set_llm_deployment_success_metrics` (already has all 4 labels via `enum_values`): `self.litellm_deployment_in_progress_requests.labels(...).dec()`.

- **Dec (failure)**: Add 1 line to `set_llm_deployment_failure_metrics` (same): `self.litellm_deployment_in_progress_requests.labels(...).dec()`.

This reuses the existing `prometheus_label_factory` + `get_labels_for_metric` label construction, so no new label code is needed. The `async_log_pre_api_call` hook fires after deployment selection (inside `litellm.completion`/`litellm.acompletion`, called by the router after `_update_kwargs_with_deployment`), so deployment labels are available.

#### 3. New endpoint module: `litellm/proxy/model_metrics_endpoints/`

File: `litellm/proxy/model_metrics_endpoints/__init__.py` (empty)
File: `litellm/proxy/model_metrics_endpoints/per_model_endpoints.py`

Follows the `analytics_endpoints.py` pattern: own `router = APIRouter()`, registered via `app.include_router(...)` in `proxy_server.py`.

```
GET /model/metrics/per_model?window=1h&model_id=<optional>
```

Parameters:
- `window` (string, required): one of `1m`, `15m`, `1h`, `24h`, `7d`
- `model_id` (string, optional): filter to a specific deployment

Response shape:

```json
{
  "prometheus_connected": true,
  "window": "1h",
  "step": "30s",
  "deployments": [
    {
      "model_id": "abc-123",
      "litellm_model_name": "Qwen3.6-35B-A3B-FP8",
      "api_base": "http://s-xxx:8000",
      "api_provider": "hosted_vllm",
      "rpm_limit": 100,
      "concurrent_requests": [{"timestamp": "...", "value": 3}, ...],
      "request_rate": [{"timestamp": "...", "value": 0.8}, ...],
      "output_tokens_per_sec": [{"timestamp": "...", "value": 120.5}, ...],
      "latency_per_token_p50": [{"timestamp": "...", "value": 0.008}, ...]
    }
  ]
}
```

When `prometheus_connected` is false, `concurrent_requests` returns the current instant value only (single-element array), and other series are empty arrays.

#### 4. Prometheus query helpers

File: `litellm/integrations/prometheus_helpers/prometheus_api.py`

Add `query_prometheus_range(query, start, end, step)` following the `get_daily_spend_from_prometheus` pattern (uses `query_range` API).

Add `get_per_model_metrics(window, model_id)` that runs 4 PromQL queries and groups results by deployment.

PromQL queries:

| Series | PromQL |
|--------|--------|
| Concurrent requests | `litellm_deployment_in_progress_requests` (instant or `max_over_time(...[window])`) |
| Request rate | `sum by (model_id, api_base, litellm_model_name, api_provider) (rate(litellm_deployment_total_requests_total[window]))` |
| Output tokens/sec | `sum by (model_id, litellm_model_name, api_provider) (rate(litellm_output_tokens_metric_total[window]))` |
| Latency per token p50 | `histogram_quantile(0.50, sum by (le, model_id, api_base, litellm_model_name, api_provider) (rate(litellm_deployment_latency_per_output_token_bucket[window])))` |

Window mapping:
- `1m` -> step `15s`
- `15m` -> step `15s`
- `1h` -> step `30s`
- `24h` -> step `5m`
- `7d` -> step `1h`

#### 5. Deploy config: set PROMETHEUS_URL

File: `oicm-litellm-layer/deploy/litellm-proxy.yaml`

```yaml
- name: PROMETHEUS_URL
  value: "http://kube-prometheus-stack-prometheus.kube-prometheus-stack:9090"
```

#### 6. Tests

File: `tests/test_litellm/integrations/prometheus/test_deployment_in_progress_requests.py`

Test the inc/dec contract via the PrometheusLogger hooks:
- `async_log_pre_api_call` then `async_log_success_event` -> gauge returns to 0
- `async_log_pre_api_call` then `async_log_failure_event` -> gauge returns to 0
- Two pre-call incs without dec -> gauge at 2
- No-op when deployment info is missing (model_id empty)

File: `tests/test_litellm/integrations/prometheus_helpers/test_per_model_metrics.py`

Test PromQL builder and response parsing with mocked HTTP responses.

### Frontend

#### 7. API call + types

File: `ui/litellm-dashboard/src/components/networking.tsx`

One new `perModelMetricsCall(accessToken, params)` using `apiClient.get`, following the existing `modelSlowResponsesCall` pattern.

#### 8. PerModelRealTimeView component

File: `ui/litellm-dashboard/src/components/UsagePage/components/ModelAnalytics/PerModelRealTimeView.tsx`

Structure:
- Time-window selector (button group): 1m, 15m, 1h, 24h, 7d
- Per-deployment card with 4 line charts (concurrent, request rate, throughput, latency) + headroom gauge
- `useQuery` with `refetchInterval`: 5s for 1m/15m, 30s for longer

#### 9. Integration into ModelAnalyticsView

Add a tab/toggle to switch between Tier 1 (SQL-backed) and Tier 2 (Prometheus-backed) views.

## What is NOT in scope

- Per-pod visibility (a registered model is treated as one deployment)
- Per-API-key or per-team breakdown of concurrent requests
- Alerting on concurrency thresholds (Alertmanager, separate effort)
- Historical data beyond Prometheus retention (7 days). For >7d, Tier 1 SQL endpoints cover that

## Effort estimate

| Item | Days |
|------|------|
| Backend: gauge + labels + inc/dec hooks | 1 |
| Backend: endpoint module + Prometheus query helpers | 1.5 |
| Backend: tests | 1 |
| Backend: deploy config | 0.5 |
| Frontend: API call + types + component | 2.5 |
| Frontend: integration | 0.5 |
| Staging validation | 1 |
| **Total** | **8** |

## Risks

1. **Inc/dec contract**: mitigated by using the `CustomLogger` hook lifecycle (proven by `keda_metrics.py` in production). `async_log_pre_api_call` always pairs with exactly one of `async_log_success_event` or `async_log_failure_event`. Tests cover all paths.

2. **Prometheus cardinality**: 4 labels, ~20 deployments = 20 series. Negligible against existing 130+ series.

3. **PROMETHEUS_URL availability**: the proxy needs to reach `kube-prometheus-stack-prometheus.kube-prometheus-stack:9090` cross-namespace. Need to verify no NetworkPolicy blocks this.

4. **Multiprocess gauge**: `multiprocess_mode="livesum"` sums across 4 Granian workers. Correct for concurrent requests. Matches the existing `litellm_in_flight_requests` gauge.

5. **Step size for 1m window**: 15s step with 30s scrape interval returns 4 data points. Sparse but usable for "current state".
