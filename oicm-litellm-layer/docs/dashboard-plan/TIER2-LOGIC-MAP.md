# Tier 2 Logic Map: Functions, Flows, and Data Contracts

Status: analysis complete.
Date: 2026-07-23.

This document maps every function, data flow, and data contract in the Tier 2 per-model real-time metrics pipeline. Each step is annotated with the exact file, function, and line number. Live data was scraped from the production Prometheus instance and saved under `live-data/` for offline testing.

## Architecture overview

```
User request
    |
    v
[Proxy] --(1)--> litellm.acompletion()
    |                    |
    |                    v
    |              [Router] picks deployment
    |                    |
    |                    v
    |              [Logging.pre_call]
    |                    |
    |                    v
    |              (2) log_pre_api_call  <-- INC gauge (BROKEN: see below)
    |                    |
    |                    v
    |              [Provider HTTP call]
    |                    |
    |              +-----+-----+
    |              |           |
    |              v           v
    |         (3a) success  (3b) failure
    |              |           |
    |              v           v
    |       async_log_    async_log_
    |       success_event failure_event
    |              |           |
    |              v           v
    |       (4a) set_llm_  (4b) set_llm_
    |       deployment_  deployment_
    |       success_     failure_
    |       metrics      metrics
    |              |           |
    |              +-----+-----+
    |                    |
    |                    v
    |              (5) gauge.dec()  <-- DEC gauge (works)
    |
    v
[Response to user]

Separate flow:
[Prometheus] scrapes /metrics/ every 30s
    |
    v
(6) Gauge values stored as time series

User queries dashboard:
[UI] --> GET /model/metrics/per_model?window=1h
    |
    v
(7) get_per_model_metrics()
    |
    v
(8) 4x query_prometheus_range() + 1x query_prometheus_instant()
    |
    v
(9) _parse_range_result() per series (filters NaN)
    |
    v
(10) Group by deployment labels, return JSON
```

## Step-by-step function map

### Step 1: Request enters the proxy

File: `litellm/proxy/proxy_server.py`
Function: `chat_completion` (line ~6936)

The FastAPI handler receives the request, calls `proxy_logging_obj.pre_call_hook`, then `router.acompletion()`. The router selects a deployment (setting `model_id`, `api_base`, `api_provider` in `litellm_params`), then calls `litellm.acompletion()`.

### Step 2: INC gauge via async_pre_call_deployment_hook (FIXED)

File: `litellm/utils.py`
Function: `async_pre_call_deployment_hook` (line ~1142)

The router calls this function after deployment selection but before the provider HTTP call. It iterates `litellm.callbacks` (which Prometheus IS registered in):

```python
for callback in litellm.callbacks:
    if isinstance(callback, CustomLogger):
        result = await callback.async_pre_call_deployment_hook(modified_kwargs, typed_call_type)
```

File: `litellm/integrations/prometheus.py`
Function: `PrometheusLogger.async_pre_call_deployment_hook` (line ~1233)

```python
async def async_pre_call_deployment_hook(self, kwargs, call_type):
    model = kwargs.get("model", "")
    self._inc_deployment_in_progress(model, kwargs)
    return None
```

Function: `_inc_deployment_in_progress` (line ~1195)

Extracts `model_id`, `litellm_model_name`, `api_provider` from `standard_logging_object` or `litellm_params.metadata.model_info`. Extracts `api_base` from `litellm_params.api_base` (normalized via `_normalize_api_base_for_gauge`). Calls `gauge.labels(...).inc()`.

**Why this works**: `async_pre_call_deployment_hook` is called from `wrapper_async` in `utils.py` (line ~1609), which is on the main `acompletion` / `aembedding` / etc. path. It fires after `function_setup` (so `litellm_params` is populated with `api_base`, `custom_llm_provider`, `metadata.model_info`) and after the router has selected a deployment (so `model_info.id` is available). It fires per-attempt, matching the per-attempt DEC in success/failure.

**Previous bug (now fixed)**: The INC was in `log_pre_api_call` / `async_log_pre_api_call`, which are never called because the logging system only iterates `litellm.input_callback` for pre-call hooks, and Prometheus is registered in `litellm.callbacks` (promoted to `success_callback`), never `input_callback`. The `async_log_pre_api_call` method on `CustomLogger` had no call site in the entire codebase. Both dead methods were removed.

### Step 3: LLM provider HTTP call

Not in scope. The provider (vLLM) processes the request and returns a response or raises an exception.

### Step 4a: DEC gauge on success

File: `litellm/litellm_core_utils/litellm_logging.py`
Function: `Logging.success_handler` (line ~2496)

Iterates over `litellm._async_success_callback` and calls `async_log_success_event`.

File: `litellm/integrations/prometheus.py`
Function: `async_log_success_event` (line ~1217)

Calls `set_llm_deployment_success_metrics`.

Function: `set_llm_deployment_success_metrics` (line ~2580)

Extracts `api_base` from `standard_logging_payload["api_base"]` and `_litellm_params.get("api_base")`. Normalizes via `_normalize_api_base_for_gauge`. Calls `gauge.labels(...).dec()`.

### Step 4b: DEC gauge on failure

File: `litellm/litellm_core_utils/litellm_logging.py`
Function: `Logging.failure_handler` (line ~2938)

Iterates over `litellm._async_failure_callback` and calls `async_log_failure_event`.

File: `litellm/integrations/prometheus.py`
Function: `async_log_failure_event`

Calls `set_llm_deployment_failure_metrics`.

Function: `set_llm_deployment_failure_metrics` (line ~2320)

Extracts `api_base` from `standard_logging_payload.get("api_base")` and `_litellm_params.get("api_base")`. Normalizes via `_normalize_api_base_for_gauge`. Calls `gauge.labels(...).dec()`.

### Step 5: Gauge state

The `litellm_deployment_in_progress_requests` gauge uses `multiprocess_mode="livesum"` which is correct for in-flight tracking across 4 Granian workers. Each worker increments/decrements independently, and Prometheus sums them.

### Step 6: Prometheus scrape

File: `oicm-litellm-layer/deploy/litellm-servicemonitor.yaml`

ServiceMonitor scrapes `/metrics/` (trailing slash) every 30s on port `http` (container port 4000). The proxy exposes metrics via `prometheus_client.generate_latest()` at the `/metrics/` endpoint.

### Step 7: Endpoint handler

File: `litellm/proxy/model_metrics_endpoints/per_model_endpoints.py`
Function: `per_model_metrics_handler` (line ~30)

```
GET /model/metrics/per_model?window=1h&model_id=<optional>
```

Mounts in `proxy_server.py` line 15792: `app.include_router(per_model_metrics_router)` (no `/v1` prefix).

Valid windows: `1m`, `15m`, `1h`, `24h`, `7d`. Auth: `user_api_key_auth`.

When Prometheus is connected, calls `get_per_model_metrics(window, model_id)`. When not connected, falls back to `get_in_progress_requests_instant()`.

### Step 8: Prometheus query helpers

File: `litellm/integrations/prometheus_helpers/prometheus_api.py`

#### `get_per_model_metrics(window, model_id)` (line ~256)

Runs 4 PromQL range queries + 1 instant query:

| Series | PromQL | Prometheus metric |
|--------|--------|-------------------|
| concurrent_requests | `max_over_time(litellm_deployment_in_progress_requests[window])` | Gauge |
| request_rate | `sum by (model_id, litellm_model_name, api_base, api_provider) (rate(litellm_deployment_total_requests_total[window]))` | Counter |
| output_tokens_per_sec | `sum by (model_id, litellm_model_name, api_base, api_provider) (rate(litellm_output_tokens_metric_total[window]))` | Counter |
| latency_per_token_p50 | `histogram_quantile(0.50, sum by (le, model_id, litellm_model_name, api_base, api_provider) (rate(litellm_deployment_latency_per_output_token_bucket[window])))` | Histogram |
| rpm_limit (instant) | `litellm_deployment_rpm_limit` | Gauge |

Window config:

```python
_WINDOW_CONFIG = {
    "1m": ("1m", "15s"),
    "15m": ("15m", "15s"),
    "1h": ("1h", "30s"),
    "24h": ("24h", "5m"),
    "7d": ("7d", "1h"),
}
```

#### `query_prometheus_range(query, start, end, step)` (line ~192)

Calls `GET {PROMETHEUS_URL}/api/v1/query_range` with Unix timestamp floats (not ISO strings). Returns `data.result` list.

#### `query_prometheus_instant(query)` (line ~213)

Calls `GET {PROMETHEUS_URL}/api/v1/query`. Returns `data.result` list.

### Step 9: Result parsing

#### `_parse_range_result(result)` (line ~228)

Converts Prometheus `[[timestamp, value], ...]` into `[{"timestamp": ISO, "value": float}, ...]`.

NaN filtering: `if parsed != parsed: continue` (NaN is the only value where `x != x` is True). This prevents `ValueError: Out of range float values are not JSON compliant: nan` when the response is serialized.

#### `_extract_deployment_key(metric_labels)` (line ~240)

Extracts `(model_id, litellm_model_name, api_base, api_provider)` from Prometheus metric labels. Used as the dict key for grouping series by deployment.

#### `_empty_deployment_dict(key)` (line ~246)

Creates the initial deployment dict with empty time-series arrays.

### Step 10: Response shape

```json
{
  "prometheus_connected": true,
  "window": "1h",
  "step": "30s",
  "deployments": [
    {
      "model_id": "abc-123",
      "litellm_model_name": "zai-org/GLM-5.2-FP8",
      "api_base": "http://vllm:8080/v1",
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

## Normalization helpers

### `_normalize_api_base_for_gauge(api_base)` (prometheus.py, line ~91)

Strips known endpoint suffixes so INC and DEC produce the same label value:

```python
_API_BASE_ENDPOINT_SUFFIXES = (
    "/chat/completions",
    "/completions",
    "/embeddings",
    "/responses",
    "/rerank",
    "/transcriptions",
    "/translations",
    "/images/generations",
    "/audio/speech",
)
```

At pre-call, `litellm_params.api_base` is the full endpoint URL (e.g. `http://host:8080/v1/chat/completions`). By success/failure time, litellm has stripped it to the base URL (`http://host:8080/v1`). Without normalization, the INC and DEC hit different label series and the gauge leaks negative.

### `_parse_window_to_timedelta(window)` (prometheus_api.py, line ~390)

Parses PromQL duration strings (`1m`, `15m`, `1h`, `24h`, `7d`, `1w`) into `timedelta`.

## Known bugs (as of 2026-07-23)

### Bug 1: INC hook never fires (FIXED)

The INC was in `log_pre_api_call` / `async_log_pre_api_call`, which are never called by the logging system. The logging system only iterates `litellm.input_callback` for pre-call hooks, but Prometheus is registered in `litellm.callbacks` (promoted to `success_callback`), never `input_callback`.

**Fix**: Moved the INC to `async_pre_call_deployment_hook`, which IS called by the router (from `wrapper_async` in `utils.py` line ~1609) after deployment selection. Removed the dead `log_pre_api_call` and `async_log_pre_api_call` overrides from `PrometheusLogger`.

### Bug 2: Stale label series from pre-normalization era

Before the `_normalize_api_base_for_gauge` fix, the INC used `/v1/chat/completions` and the DEC used `/v1`. These old label series still have negative values in Prometheus and will persist until the staleness window expires (5 minutes of no scrapes). The normalization fix prevents new mismatches but cannot clean up old data.

### Bug 3: NaN values in histogram_quantile

`histogram_quantile(0.50, ...)` returns NaN when there are no bucket samples in the window. The `_parse_range_result` NaN filter handles this by skipping NaN data points, but entire series may appear empty.
