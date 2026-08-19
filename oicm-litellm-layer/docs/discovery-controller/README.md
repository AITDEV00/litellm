# Discovery Controller

The OICM discovery controller is a Kubernetes sidecar that discovers model deployments (local and cross-cluster via Submariner) and registers them as LiteLLM models through the LiteLLM proxy REST API. It runs as a separate Deployment in the `mlops` namespace, watching the `adeo` namespace for model-serving workloads.

## What It Does

The controller discovers models from two sources:

1. **Local deployments**. Kubernetes Deployments in the `adeo` namespace labeled with `oip/workload-type=model_deployment`. The controller reads the `MODEL_ID` from a ConfigMap or queries the model server's `/v1/models` endpoint, then registers it in LiteLLM with an `api_base` pointing at the in-cluster service (`s-{uuid}.adeo.svc.cluster.local:8080`).

2. **Submariner cross-cluster imports**. EndpointSlices created by the Submariner Lighthouse agent that carry the `oip/workload-type=model_deployment` label. These represent services exported from remote clusters (e.g. Abu Dhabi) and made reachable via Submariner globalnet IPs. The controller queries each remote model's `/v1/models` endpoint through the globalnet IP and registers it in LiteLLM with an `api_base` pointing at the globalnet IP (`http://242.0.0.x:8080/v1`).

Both sources are polled on every full sync cycle (default every 300 seconds). Local deployments are also watched in real-time via the Kubernetes watch API for immediate add/delete/modify events.

## Documents

| Document | Description |
|---|---|
| [logic-map-and-code-smell-audit.md](logic-map-and-code-smell-audit.md) | Full logic mapping (Phase 1: Trace with ASCII flow diagrams and file:line refs) and code smell detection (L1-L4) of the `controller/` package |

## Related Resources

| Resource | Location | Description |
|---|---|---|
| Controller source README | `controller/README.md` | Developer docs for the controller package: architecture (VSA), dependency graph, how to add a new source, Submariner import discovery |
| Deployment manifest | `deploy/prod/discovery-controller.yaml` | K8s Deployment + RBAC + ServiceAccount. Pinned to `adeo-gpu-03` (Submariner gateway node). Includes extensive comments on node pinning, tolerations, and RBAC for EndpointSlices |
| Implementation plan | `docs/architecture/IMPLEMENTATION_PLAN.md` (Component #1) | Original design document for the discovery controller: scope, architecture, data flow, deployment steps |
| Pricing logic map | `docs/model-pricing/PRICING-LOGIC-MAP.md` | Function-level trace of pricing resolution, which is triggered by the discovery controller's `_handle_add` event handler |
| Changelog | `CHANGELOG.md` | Multiple entries covering the VSA rewrite, dedup fix, concurrent batch HTTP, health server fix, and secret reference fix |
| Admin API dedup guide | `docs/admin-api/LITELLM-ADMIN-REST-API.md` (§"OICM Discovery Controller") | How the discovery controller uses `oicm_uuid` for model deduplication via the LiteLLM admin API |

## Architecture

The code follows vertical slice architecture (VSA). Each source is a self-contained module implementing the `ModelSource` ABC. The controller orchestrates them without knowing their internals.

```
controller/
  __main__.py              Entry point. Signal handling, event loop.
  config.py                Environment variables, constants, logging setup.
  models.py                OicmModel dataclass, sanitize_model_id, detect_mode.
  litellm_client.py        LiteLLMClient. Batch register/deregister/patch via REST API.
  reconciler.py            SyncReconciler. Pure compute_plan + execute. Dedup logic.
  controller.py            DiscoveryController. Orchestration: start/stop, full_sync,
                           watch loop, event handlers, health endpoint.
  sources/
    __init__.py            Exports ModelSource.
    base.py                ModelSource ABC. Single method: discover() -> Dict[str, OicmModel].
    local_deployments.py   LocalDeploymentSource. Watches K8s Deployments + ConfigMaps.
    submariner_imports.py  SubmarinerImportSource. Reads lighthouse EndpointSlices.
  fallbacks/
    __init__.py            Exports FallbackReconciler.
    client.py              FallbackClient. Reads/writes LiteLLM fallback config.
    service.py             FallbackReconciler. Validates existing fallbacks.
  pricing/
    __init__.py            Exports PricingResolver, PricingSource, etc.
    models.py              PricingEntry, MatcherCandidate, PricingResult (frozen).
    resolver.py            PricingResolver: orchestrates matchers.
    source.py              PricingSource: loads JSON, builds PricingIndex.
    matchers.py            exact, structured, fuzzy, substring matchers.
    normalizer.py          Model name normalization.
    aggregator.py          Weighted aggregation of candidates.
    utils.py               pricing_to_params converter.
```

### Dependency graph (strictly one-directional, no cycles)

```
__main__ -> controller -> sources (base, local_deployments, submariner_imports)
                        -> reconciler -> litellm_client -> models
                        -> fallbacks (service -> client)
                        -> pricing (resolver -> source, matchers, normalizer, aggregator)
                        -> models, config
sources -> models, config
```

## Deployment

```bash
# Build and push the controller image (requires Harbor access, run from oicm-litellm-layer/)
make login && make build && make push-discovery

# Deploy to the cluster
kubectl apply -f deploy/prod/discovery-controller.yaml

# Restart to pull a new image
kubectl -n mlops rollout restart deploy/oicm-discovery-controller
```

The controller is pinned to `adeo-gpu-03` (the Submariner gateway node) because it queries Submariner-imported model endpoints at globalnet IPs (`242.0.0.x`), which are only reachable from the gateway node due to Cilium's BPF kube-proxy replacement dropping return traffic for non-gateway pods.

## Key Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ADEO_NAMESPACE` | `adeo` | Namespace to watch for model deployments |
| `LITELLM_ADMIN_URL` | (required) | LiteLLM proxy admin API URL |
| `LITELLM_ADMIN_KEY` | (required) | LiteLLM proxy admin key |
| `SYNC_INTERVAL` | `300` | Full sync interval in seconds |
| `HEALTH_PORT` | `8090` | Health server port |
| `ENABLE_SUBMARINER_IMPORTS` | `false` | Enable cross-cluster Submariner import discovery |
| `ENABLE_FALLBACKS` | `false` | Enable fallback configuration reconciliation |
| `ENABLE_PRICING` | `false` | Enable automatic pricing resolution for discovered models |
