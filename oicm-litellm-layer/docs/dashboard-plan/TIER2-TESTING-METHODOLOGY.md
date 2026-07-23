# Tier 2 Testing Methodology: Live Data Analysis

Status: complete.
Date: 2026-07-23.

## How to reproduce

### Prerequisites

1. SSH tunnel to the cluster:
```bash
sshpass -p 'Password123' ssh -fN -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -L 6443:10.34.104.10:6443 adeo@10.34.104.99
```

2. Port-forward Prometheus:
```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl --server=https://localhost:6443 --insecure-skip-tls-verify=true -n kube-prometheus-stack port-forward svc/kube-prometheus-stack-prometheus 9090:9090 &
```

3. Port-forward the proxy pod:
```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl --server=https://localhost:6443 --insecure-skip-tls-verify=true -n mlops port-forward litellm-proxy-<pod> 4002:4000 &
```

### Scrape live data

All live data was scraped on 2026-07-23 and saved to `live-data/`. To re-scrape:

```bash
DATA_DIR="oicm-litellm-layer/docs/dashboard-plan/live-data"
PROM="http://localhost:9090/api/v1"
NOW=$(date +%s)
START=$((NOW - 3600))

# Instant queries
curl -s -G "$PROM/query" --data-urlencode 'query={__name__=~"litellm_.*"}' > "$DATA_DIR/01-all-litellm-metrics-instant.json"
curl -s -G "$PROM/query" --data-urlencode 'query=litellm_deployment_in_progress_requests' > "$DATA_DIR/02-deployment-in-progress-instant.json"

# Range queries (1h, 30s step)
curl -s "$PROM/query_range?query=litellm_deployment_in_progress_requests&start=$START&end=$NOW&step=30s" > "$DATA_DIR/03-deployment-in-progress-range-1h.json"
curl -s -G "$PROM/query_range" --data-urlencode 'query=sum by (model_id,litellm_model_name,api_base,api_provider) (rate(litellm_deployment_total_requests_total[1h]))' --data-urlencode "start=$START" --data-urlencode "end=$NOW" --data-urlencode "step=30s" > "$DATA_DIR/04-request-rate-range-1h.json"
curl -s -G "$PROM/query_range" --data-urlencode 'query=sum by (model_id,litellm_model_name,api_base,api_provider) (rate(litellm_output_tokens_metric_total[1h]))' --data-urlencode "start=$START" --data-urlencode "end=$NOW" --data-urlencode "step=30s" > "$DATA_DIR/05-output-tokens-per-sec-range-1h.json"
curl -s -G "$PROM/query_range" --data-urlencode 'query=histogram_quantile(0.50, sum by (le,model_id,litellm_model_name,api_base,api_provider) (rate(litellm_deployment_latency_per_output_token_bucket[1h])))' --data-urlencode "start=$START" --data-urlencode "end=$NOW" --data-urlencode "step=30s" > "$DATA_DIR/06-latency-per-token-p50-range-1h.json"

# Instant queries for limits and state
curl -s -G "$PROM/query" --data-urlencode 'query=litellm_deployment_rpm_limit' > "$DATA_DIR/07-rpm-limit-instant.json"
curl -s -G "$PROM/query" --data-urlencode 'query=litellm_deployment_state' > "$DATA_DIR/08-deployment-state-instant.json"

# Proxy pod raw metrics
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl --server=https://localhost:6443 --insecure-skip-tls-verify=true exec -n mlops litellm-proxy-<pod> -- python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:4000/metrics/').read().decode())" > "$DATA_DIR/11-raw-pod-metrics.txt"

# Endpoint response
curl -s "http://localhost:4002/model/metrics/per_model?window=1h" -H "Authorization: Bearer sk-1234" > "$DATA_DIR/12-per-model-endpoint-response-1h.json"
curl -s "http://localhost:4002/model/metrics/per_model?window=1m" -H "Authorization: Bearer sk-1234" > "$DATA_DIR/13-per-model-endpoint-response-1m.json"
```

### Load live data for offline testing

```python
import json
from pathlib import Path

LIVE_DATA = Path("oicm-litellm-layer/docs/dashboard-plan/live-data")

def load_live_response(filename: str) -> dict | list:
    with open(LIVE_DATA / filename) as f:
        return json.load(f)

# Example: load the instant gauge values
gauge_data = load_live_response("02-deployment-in-progress-instant.json")
for result in gauge_data["data"]["result"]:
    labels = result["metric"]
    value = float(result["value"][1])
    print(f'{labels.get("litellm_model_name", "")}: {value}')
```

## Live data analysis results (2026-07-23)

### Metric inventory

52 unique litellm metric names, 5023 total time series. Key metrics for Tier 2:

| Metric | Type | Series count | Used for |
|--------|------|-------------|----------|
| `litellm_deployment_in_progress_requests` | Gauge (livesum) | 16 | Concurrent requests |
| `litellm_deployment_total_requests_total` | Counter | 44 | Request rate |
| `litellm_output_tokens_metric_total` | Counter | 32 | Output tokens/sec |
| `litellm_deployment_latency_per_output_token_bucket` | Histogram | 615 | Latency per token p50 |
| `litellm_deployment_rpm_limit` | Gauge | 64 | RPM limit |
| `litellm_deployment_state` | Gauge | 79 | Healthy/unhealthy |

### Step 1: Instant gauge test

**Query**: `litellm_deployment_in_progress_requests`
**File**: `02-deployment-in-progress-instant.json`

Results: 15 series total
- Negative: 9 (stale from pre-normalization era + INC hook not firing)
- Zero: 4
- Positive: 2 (in-flight requests)

**Finding**: The gauge is still going negative because the INC hook (`log_pre_api_call`) is never called. The DEC hook fires correctly on success/failure, but without the matching INC, values drift negative.

### Step 2: Request rate test

**Query**: `sum by (model_id,litellm_model_name,api_base,api_provider) (rate(litellm_deployment_total_requests_total[1h]))`
**File**: `04-request-rate-range-1h.json`

Results: 26 series. Values range from 0 to ~0.68 req/sec. Working correctly.

### Step 3: Output tokens per sec test

**Query**: `sum by (model_id,litellm_model_name,api_base,api_provider) (rate(litellm_output_tokens_metric_total[1h]))`
**File**: `05-output-tokens-per-sec-range-1h.json`

Results: 14 series. Values range from 0 to ~182 tokens/sec. Working correctly.

### Step 4: Latency per token p50 test

**Query**: `histogram_quantile(0.50, sum by (le,model_id,litellm_model_name,api_base,api_provider) (rate(litellm_deployment_latency_per_output_token_bucket[1h])))`
**File**: `06-latency-per-token-p50-range-1h.json`

Results: 18 series. Some values are NaN (filtered by `_parse_range_result`). Working correctly after NaN filter.

### Step 5: Per-model endpoint test

**Endpoint**: `GET /model/metrics/per_model?window=1h`
**File**: `12-per-model-endpoint-response-1h.json`

Results: 200 OK, 45 deployments returned. `prometheus_connected: true`. All 4 time-series present. NaN values filtered out. No 500 error.

**Issues found**:
- `concurrent_requests` values are negative (due to INC hook bug)
- `output_tokens_per_sec` is empty for some deployments (no output token metrics for those deployments)
- Some deployments have empty `litellm_model_name` (label missing in Prometheus)

### Step 6: Live inc/dec test

**Test**: Send a request to `zai-org/GLM-5.2-FP8`, capture gauge before and after.
**Files**: `14-gauge-before-request.json`, `15-gauge-after-request.json`, `14b-test-request-response.json`

Result: Request succeeded (24 tokens). Gauge delta = 0.0 (no change). This confirms the INC hook is not firing.

## Expected values after fix

After fixing the INC hook (Slice 1 in the VSA plan):

| Metric | Expected value | How to verify |
|--------|---------------|---------------|
| Gauge (idle deployment) | 0.0 | `curl -s -G http://localhost:9090/api/v1/query --data-urlencode "query=litellm_deployment_in_progress_requests"` |
| Gauge (in-flight request) | >= 1.0 | Send a slow request, check gauge during |
| Gauge (after request completes) | 0.0 | Send a request, wait, check gauge |
| Per-model endpoint concurrent_requests | 0.0 or positive | `curl -s http://localhost:4002/model/metrics/per_model?window=1m` |
| Per-model endpoint request_rate | >= 0.0 | Same endpoint, 1h window |
| Per-model endpoint output_tokens_per_sec | >= 0.0 | Same endpoint, 1h window |
| Per-model endpoint latency_per_token_p50 | >= 0.0 or empty | Same endpoint, 1h window |
