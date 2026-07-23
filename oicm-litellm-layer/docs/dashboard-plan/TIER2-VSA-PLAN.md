# Tier 2 Rebuild: Modularized Vertical Slice Architecture

Status: plan.
Date: 2026-07-23.

This document defines the rebuild of Tier 2 per-model metrics using modularized vertical slice architecture (VSA). Each slice is a self-contained feature that can be built, tested, and deployed independently. The plan borrows as many functions as possible from the existing litellm codebase.

## Design principles

1. Each slice is a complete vertical: endpoint -> query -> parse -> response -> test
2. Slices are ordered by dependency: each slice builds on the previous one
3. Every function is tested against live Prometheus data (scraped to `live-data/`)
4. No new abstractions for single-use code
5. Reuse existing litellm patterns: `APIRouter`, `user_api_key_auth`, `prometheus_label_factory`, `get_async_httpx_client`
6. Each slice has a clear "definition of done" that can be verified with a curl command

## Existing code to borrow from

| Pattern | Source file | What to reuse |
|---------|------------|---------------|
| Extracted router | `litellm/proxy/analytics_endpoints/analytics_endpoints.py` | `APIRouter()` + `app.include_router()` pattern |
| Prometheus query | `litellm/integrations/prometheus_helpers/prometheus_api.py` | `query_prometheus_range`, `query_prometheus_instant` |
| Label construction | `litellm/integrations/prometheus_helpers/prometheus_label_factory.py` | `prometheus_label_factory`, `UserAPIKeyLabelValues` |
| HTTP client | `litellm/llms/custom_httpx/http_handler.py` | `get_async_httpx_client(llm_provider=httpxSpecialProvider.LoggingCallback)` |
| Auth dependency | `litellm/proxy/auth/user_api_key_auth.py` | `user_api_key_auth` FastAPI dependency |
| Config env var | `litellm/integrations/prometheus_helpers/prometheus_api.py` | `PROMETHEUS_URL = get_secret("PROMETHEUS_URL")` |
| NaN filter | `litellm/integrations/prometheus_helpers/prometheus_api.py` | `_parse_range_result` with NaN skip |
| API base normalization | `litellm/integrations/prometheus.py` | `_normalize_api_base_for_gauge` |
| Gauge factory | `litellm/integrations/prometheus.py` | `_gauge_factory` with `multiprocess_mode="livesum"` |
| Window parsing | `litellm/integrations/prometheus_helpers/prometheus_api.py` | `_parse_window_to_timedelta`, `_WINDOW_CONFIG` |

## Slice 1: Fix the gauge INC hook

Status: complete.

**Goal**: The `litellm_deployment_in_progress_requests` gauge must INC before a request and DEC after it completes, returning to 0.

**Problem**: The INC hook (`log_pre_api_call`) was never called because Prometheus is registered as a `success_callback`, not an `input_callback`. The logging system only iterates `litellm.input_callback` for pre-call hooks.

**Fix applied**: Moved the INC into `async_pre_call_deployment_hook`, which IS called by the router (from `wrapper_async` in `utils.py` line ~1609) after deployment selection. Removed the dead `log_pre_api_call` and `async_log_pre_api_call` overrides.

Why `async_pre_call_deployment_hook` is the correct hook:
- It fires after deployment selection (so `model_id`, `api_base` are available)
- It fires per-attempt (matching the per-attempt DEC in success/failure)
- It is in the `CustomLogger` base class and already invoked by the router

**Files to modify**:

1. `litellm/integrations/prometheus.py`:
   - Added `async_pre_call_deployment_hook(self, kwargs, call_type)` that calls `_inc_deployment_in_progress`
   - Removed dead `log_pre_api_call` and `async_log_pre_api_call` overrides

2. `tests/test_litellm/integrations/test_prometheus_deployment_in_progress_requests.py`:
   - All tests updated to call `async_pre_call_deployment_hook` instead of `async_log_pre_api_call`
   - Added test: `test_deployment_hook_incs_gauge_with_only_litellm_params` (production scenario: no `standard_logging_object`, only `litellm_params.metadata.model_info`)
   - Removed test: `test_sync_log_pre_api_call_incs_gauge` (method deleted)
   - 15 tests total, all passing

**Definition of done**:
```bash
# Send a request, check gauge before and after
curl -s -G "http://localhost:9090/api/v1/query" --data-urlencode "query=litellm_deployment_in_progress_requests" | python3 -m json.tool
# Gauge should show 0 for idle deployments, 1+ for in-flight, never negative
```

**Test data**: `live-data/14-gauge-before-request.json`, `live-data/15-gauge-after-request.json`

## Slice 2: Prometheus query layer

**Goal**: A typed, tested module that queries Prometheus and returns parsed Python objects.

**This already exists** in `prometheus_api.py`. The functions are:
- `query_prometheus_range(query, start, end, step)` -> `list[dict]` (raw Prometheus result)
- `query_prometheus_instant(query)` -> `list[dict]` (raw Prometheus result)
- `_parse_range_result(result)` -> `list[dict]` (flattened `[{timestamp, value}]`, NaN filtered)
- `_parse_window_to_timedelta(window)` -> `timedelta`
- `is_prometheus_connected()` -> `bool`

**Borrow from**: `get_daily_spend_from_prometheus` (line 99) for the `query_range` pattern.

**Tests**: `tests/test_litellm/integrations/test_prometheus_per_model_api.py` (15 tests, all passing)

**Test data**: `live-data/03-deployment-in-progress-range-1h.json`, `live-data/04-request-rate-range-1h.json`, `live-data/05-output-tokens-per-sec-range-1h.json`, `live-data/06-latency-per-token-p50-range-1h.json`

**Definition of done**: All 15 tests pass. Each PromQL query returns results when run against live Prometheus.

## Slice 3: Per-model metrics aggregation

**Goal**: A function that runs all 4 PromQL queries + 1 instant query, groups results by deployment, and returns a structured dict.

**This already exists**: `get_per_model_metrics(window, model_id)` in `prometheus_api.py` (line 256).

**Data contract**:
```python
{
    "prometheus_connected": bool,
    "window": str,           # "1h"
    "step": str,             # "30s"
    "deployments": [
        {
            "model_id": str,
            "litellm_model_name": str,
            "api_base": str,
            "api_provider": str,
            "rpm_limit": int,
            "concurrent_requests": list[dict],   # [{timestamp, value}]
            "request_rate": list[dict],
            "output_tokens_per_sec": list[dict],
            "latency_per_token_p50": list[dict],
        }
    ]
}
```

**PromQL queries** (all use `_DEPLOYMENT_LABELS = ("model_id", "litellm_model_name", "api_base", "api_provider")`):

| Metric | PromQL |
|--------|--------|
| concurrent_requests | `max_over_time(litellm_deployment_in_progress_requests{label_filter}[range_str])` |
| request_rate | `sum by (model_id,litellm_model_name,api_base,api_provider) (rate(litellm_deployment_total_requests_total{label_filter}[range_str]))` |
| output_tokens_per_sec | `sum by (model_id,litellm_model_name,api_base,api_provider) (rate(litellm_output_tokens_metric_total{label_filter}[range_str]))` |
| latency_per_token_p50 | `histogram_quantile(0.50, sum by (le,model_id,litellm_model_name,api_base,api_provider) (rate(litellm_deployment_latency_per_output_token_bucket{label_filter}[range_str])))` |
| rpm_limit | `litellm_deployment_rpm_limit{label_filter}` (instant) |

**Definition of done**:
```bash
curl -s "http://localhost:4002/model/metrics/per_model?window=1h" -H "Authorization: Bearer sk-1234" | python3 -m json.tool
# Returns 200 with deployments array, no NaN values, no 500 error
```

**Test data**: `live-data/12-per-model-endpoint-response-1h.json`

## Slice 4: Fallback path (no Prometheus)

**Goal**: When `PROMETHEUS_URL` is not set, return the instant gauge value from the proxy's own `/metrics/` endpoint.

**This already exists**: `get_in_progress_requests_instant()` in `prometheus_api.py` (line 321) and the fallback in `per_model_endpoints.py` (line ~60).

**Definition of done**:
```bash
# With PROMETHEUS_URL unset:
curl -s "http://localhost:4002/model/metrics/per_model?window=1h" -H "Authorization: Bearer sk-1234"
# Returns {"prometheus_connected": false, ..., "deployments": [{"concurrent_requests": [{"timestamp": "", "value": 0}]}]}
```

## Slice 5: UI component

**Goal**: A React component that renders the per-model metrics as a dashboard with auto-refresh.

**This already exists**: `ui/litellm-dashboard/src/components/UsagePage/components/PerModelRealTime/PerModelRealTimeView.tsx`

Features:
- Window selector: 1m, 15m, 1h, 24h, 7d
- Auto-refresh every 15s
- One `DeploymentCard` per deployment
- Shows: Concurrent Now, Request Rate, Output Tokens/sec, Latency/Token p50, RPM Limit
- Raw time-series points in a `<details>` block
- "Prometheus not connected" alert

**API client**: `perModelMetricsCall(accessToken, { window, model_id })` in `networking.tsx` (line ~8007)

**Definition of done**: Navigate to `http://localhost:4000/ui/?page=usage`, click "Real-Time Per Model" tab, see deployment cards with live data.

## Build order

```
Slice 1 (fix gauge INC) ──────────────────────> verify gauge returns to 0
    |
    v
Slice 2 (query layer) ────────────────────────> verify PromQL returns data
    |
    v
Slice 3 (aggregation) ────────────────────────> verify endpoint returns 200
    |
    v
Slice 4 (fallback) ───────────────────────────> verify fallback works
    |
    v
Slice 5 (UI) ─────────────────────────────────> verify dashboard renders
```

Each slice is independently testable using the live data files in `live-data/`.

## Testing methodology

### Unit tests

Each function has unit tests with mocked Prometheus responses:

| Test file | Tests | What it covers |
|-----------|-------|----------------|
| `test_prometheus_deployment_in_progress_requests.py` | 14 | Gauge inc/dec contract, label normalization, NaN filtering |
| `test_prometheus_per_model_api.py` | 16 | PromQL parsing, window parsing, NaN filtering, HTTP error handling, timestamp format |
| `test_per_model_endpoints.py` | 4 | Endpoint validation, window validation, fallback path |

### Integration tests against live Prometheus

The `live-data/` directory contains scraped Prometheus responses that can be loaded as test fixtures:

```python
import json
from pathlib import Path

LIVE_DATA = Path(__file__).parent / "live-data"

def load_live_response(filename: str) -> dict:
    with open(LIVE_DATA / filename) as f:
        return json.load(f)
```

### End-to-end verification

```bash
# 1. Start port-forwards
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl --server=https://localhost:6443 --insecure-skip-tls-verify=true -n kube-prometheus-stack port-forward svc/kube-prometheus-stack-prometheus 9090:9090 &
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl --server=https://localhost:6443 --insecure-skip-tls-verify=true -n mlops port-forward litellm-proxy-<pod> 4002:4000 &

# 2. Send a test request
curl -s -X POST "http://localhost:4002/v1/chat/completions" \
  -H "Authorization: Bearer sk-1234" \
  -H "Content-Type: application/json" \
  -d '{"model":"zai-org/GLM-5.2-FP8","messages":[{"role":"user","content":"Say hello"}],"max_tokens":10}'

# 3. Check the gauge
curl -s -G "http://localhost:9090/api/v1/query" --data-urlencode "query=litellm_deployment_in_progress_requests" | python3 -m json.tool

# 4. Check the endpoint
curl -s "http://localhost:4002/model/metrics/per_model?window=1h" -H "Authorization: Bearer sk-1234" | python3 -m json.tool

# 5. Check the UI
# Open http://localhost:4000/ui/?page=usage and click "Real-Time Per Model"
```
