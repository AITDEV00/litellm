# Tier 0 Implementation: Enable Prometheus Metrics

Status: complete.
Date: 2026-07-22.

Tier 0 is the prerequisite for Tier 2. It wires up the Prometheus metrics pipeline so the proxy emits metrics on `/metrics` and Prometheus scrapes them. No code changes; only config and cluster manifests.

---

## Current state

### Proxy config (`deploy/litellm-proxy.yaml`, ConfigMap `litellm-config`)

The deployed config has:

```yaml
litellm_settings:
  callbacks: litellm_hooks.vllm_param_injector.vllm_param_injector
```

`callbacks` is a single string, not a list. The `initialize_callbacks_on_proxy` function in `litellm/proxy/common_utils/callback_utils.py` handles this via its `else` branch (line 308): when `value` is not a list, it wraps it in `get_instance_fn()` and assigns it to `litellm.callbacks`. This works for the custom hook, but `"prometheus"` is never checked, so the Prometheus metrics endpoint is never mounted.

### `/metrics` endpoint behavior

The `PrometheusAuthMiddleware` (in `litellm/proxy/middleware/prometheus_auth_middleware.py`) is hardcoded into the app at `proxy_server.py:1766`:

```python
app.add_middleware(PrometheusAuthMiddleware)
```

This middleware intercepts all requests to paths containing `/metrics` and runs `user_api_key_auth` on them. When the `prometheus` callback is not registered, the `/metrics` route does not exist, but the middleware still fires first. This produces confusing behavior:

- Without auth: returns `401` with `"Unauthorized access to metrics endpoint: Authentication Error, Malformed API Key passed in."`
- With valid auth: returns `404` with `{"detail":"Not Found"}` (auth passes, but the route doesn't exist because `_mount_metrics_endpoint()` was never called)

### Prometheus in the cluster

The ADEO cluster runs `kube-prometheus-stack` with 2 Prometheus replicas (215+ days uptime). Prometheus's `serviceMonitorSelector` is `{}` (empty), meaning it discovers all ServiceMonitors in all namespaces. There are already two ServiceMonitors in the `mlops` namespace:

- `oicm-api-gateway-service-monitor` — scrapes `app.service: oicm-api-gateway` on port `8080`, path `/prometheus-metrics`, interval `30s`
- `mlops-flask-be-monitor` — scrapes `app.service: mlops-flask-be-metrics` on port `http`, path `/prometheus-metrics`, interval `30s`

Both carry the label `release: prometheus`.

### litellm-proxy Service

The `litellm-proxy` Service in `mlops` namespace has:
- No labels at all (empty `metadata.labels`)
- Port named `http` on `4000`
- Selector `app: litellm-proxy`

The ServiceMonitor needs to match this Service by label. Since the Service has no labels today, we either add labels to the Service or use a selector that matches the existing `app: litellm-proxy` pod selector (the ServiceMonitor's `selector` matches Service labels, not pod labels).

### Multi-worker consideration

The deployed proxy runs with `--run_granian --num_workers 4`. When `"prometheus"` is added to callbacks and `num_workers > 1`, `ProxyInitializationHelpers._maybe_setup_prometheus_multiproc_dir()` in `litellm/proxy/proxy_cli.py` (line 527) auto-creates `PROMETHEUS_MULTIPROC_DIR` under `/tmp/litellm_prometheus_multiproc`. This is needed because `prometheus_client` uses process-local counters by default; with multiple workers, each worker has its own counters and the multiprocess collector merges them when `/metrics` is scraped.

This happens automatically. No env var needs to be set manually.

---

## Implementation steps

### Step 1: Add `prometheus` to callbacks in the proxy config

File: `deploy/litellm-proxy.yaml`, inside the `litellm-config` ConfigMap.

Change `callbacks` from a string to a list and add `"prometheus"`:

```yaml
litellm_settings:
  callbacks:
    - litellm_hooks.vllm_param_injector.vllm_param_injector
    - prometheus
```

Also add `require_auth_for_metrics_endpoint: false` so Prometheus can scrape without a bearer token (Prometheus does not send auth headers):

```yaml
litellm_settings:
  callbacks:
    - litellm_hooks.vllm_param_injector.vllm_param_injector
    - prometheus
  require_auth_for_metrics_endpoint: false
```

### Step 2: Add labels to the litellm-proxy Service

The ServiceMonitor selector matches Service labels. The current Service has no labels. Add a label so the ServiceMonitor can find it.

In `deploy/litellm-proxy.yaml`, the Service spec:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: litellm-proxy
  namespace: mlops
  labels:
    app: litellm-proxy
spec:
  selector:
    app: litellm-proxy
  ports:
    - port: 4000
      targetPort: http
      name: http
```

The `app: litellm-proxy` label on the Service is what the ServiceMonitor will match.

### Step 3: Create a ServiceMonitor for litellm-proxy

Create a new file: `deploy/litellm-servicemonitor.yaml`

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: litellm-proxy-service-monitor
  namespace: mlops
  labels:
    release: prometheus
spec:
  endpoints:
    - interval: 30s
      path: /metrics
      port: http
  namespaceSelector:
    matchNames:
      - mlops
  selector:
    matchLabels:
      app: litellm-proxy
```

Key decisions:
- `path: /metrics` (litellm mounts the prometheus_client ASGI app at `/metrics`)
- `port: http` (matches the port name in the Service spec)
- `interval: 30s` (matches the existing ServiceMonitors)
- `labels.release: prometheus` (not strictly required since Prometheus's `serviceMonitorSelector` is `{}`, but consistent with every other ServiceMonitor in the cluster)

### Step 4: Add the ServiceMonitor to the deploy Makefile target

In `oicm-litellm-layer/Makefile`, the `deploy` target currently applies two files. Add the ServiceMonitor:

```makefile
deploy:
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/oicm-local.conf} kubectl apply -f deploy/discovery-controller.yaml
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/oicm-local.conf} kubectl apply -f deploy/litellm-proxy.yaml
	KUBECONFIG=$${KUBECONFIG:-$$HOME/.kube/oicm-local.conf} kubectl apply -f deploy/litellm-servicemonitor.yaml
```

### Step 5: Apply the changes and restart

```bash
cd oicm-litellm-layer
make deploy
kubectl rollout restart deployment/litellm-proxy -n mlops
```

### Step 6: Verify

1. Check that `/metrics` returns prometheus-formatted data:

```bash
kubectl port-forward svc/litellm-proxy -n mlops 4000:4000
curl -s http://localhost:4000/metrics | head -20
```

Expected: lines like `# HELP litellm_proxy_total_requests_metric ...` and `litellm_proxy_total_requests_metric{...} 0`.

2. Check that Prometheus is scraping the target:

```bash
kubectl port-forward svc/kube-prometheus-stack-prometheus -n kube-prometheus-stack 9090:9090
```

Open `http://localhost:9090/targets` and look for `litellm-proxy` in the service-monitors section. The endpoint should show as UP.

3. Query a metric in Prometheus:

Open `http://localhost:9090/api/v1/query?query=litellm_proxy_total_requests_metric` or use the Prometheus UI at `http://localhost:9090/graph` and query `litellm_proxy_total_requests_metric`.

4. Check that the multiproc dir was created:

```bash
kubectl -n mlops exec <pod-name> -- ls -la /tmp/litellm_prometheus_multiproc/
```

Expected: files like `counter_...`, `gauge_...`, `histogram_...` (one per worker process).

5. Check that the vllm_param_injector callback still works:

```bash
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer sk-1234" | head -5
```

The models list should still be populated (the custom hook still loads).

---

## What metrics will be available

Once Tier 0 is done, Prometheus will scrape these litellm metrics (non-exhaustive; the full list is in `litellm/integrations/prometheus.py`):

- `litellm_proxy_total_requests_metric` — total client-side requests (Counter)
- `litellm_proxy_failed_requests_metric` — failed responses (Counter)
- `litellm_deployment_total_requests` — requests per deployment, labeled with `model_id`, `api_base`, `model_group`, `api_provider` (Counter)
- `litellm_deployment_latency_per_output_token` — latency per output token per deployment (Histogram)
- `litellm_remaining_requests_metric` — RPM remaining in current window (Gauge)
- `litellm_remaining_tokens_metric` — TPM remaining in current window (Gauge)
- `litellm_deployment_state` — deployment healthy/unhealthy (Gauge)
- `litellm_deployment_cool_down_time` — cool-down remaining (Gauge)
- Various budget, rate-limit, and team/user/org spend gauges

Labels on per-deployment metrics include `model_id`, `api_base`, `model_group`, `api_provider`, which is the per-replica granularity Tier 2 needs.

---

## Risks and mitigations

### Risk 1: `/metrics` exposed without auth

Setting `require_auth_for_metrics_endpoint: false` makes `/metrics` publicly readable. In the ADEO cluster, the litellm-proxy Service is `ClusterIP` (not exposed externally), so only in-cluster workloads can reach it. The Ingress at `litellm.adeoaiengine.ecouncil.ae` could expose it if the Ingress routes `/metrics` — check the Ingress rules. If it does, add an Ingress rule to block `/metrics` or keep auth on and configure Prometheus to send a bearer token.

### Risk 2: Multiprocess counter drift

With 4 Granian workers, each worker has its own process space. The `prometheus_client` multiprocess mode uses a shared directory (`PROMETHEUS_MULTIPROC_DIR`) to aggregate counters across workers. If a worker crashes without cleanup, its counters may leak. LiteLLM handles this via `litellm/proxy/prometheus_cleanup.py` which calls `multiprocess.mark_process_dead()` on worker exit. This is automatic.

### Risk 3: Callback string vs list

The current config uses `callbacks:` as a string. Changing it to a list is a format change. The `initialize_callbacks_on_proxy` function handles both formats (list via the main branch, string via the `else` branch at line 308). When changed to a list, both callbacks (`vllm_param_injector` and `prometheus`) will be processed through the list branch, which calls `get_instance_fn()` for dotted names and checks `_known_custom_logger_compatible_callbacks` for known names. Both paths work correctly.

### Risk 4: Prometheus storage volume

The existing Prometheus instances have 215+ days of uptime and are already scraping ~20 targets. Adding one more ServiceMonitor with a 30s interval and the litellm metric cardinality (per-deployment, per-model, per-api_base labels) will increase storage usage. This is typically not a concern for a single additional target, but if there are many deployments, the cardinality of `litellm_deployment_total_requests` (one series per `model_id + api_base + model_group + api_provider` combination) could be significant. Monitor Prometheus storage after enabling.
