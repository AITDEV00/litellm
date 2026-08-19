# Discovery Controller (component #1)

A Kubernetes sidecar that discovers model deployments (local + Submariner
cross-cluster) and registers/deregisters them as LiteLLM models via the proxy
REST API. Runs as the `oicm-discovery-controller` Deployment in `mlops`.

## Source layout

| File | Purpose |
|------|---------|
| `controller/__main__.py` | Entry point, signal handling, event loop |
| `controller/config.py` | Env vars, constants, `LITELLM_ADMIN_KEY` (env wins; local fallback reads `deploy/prod/litellm-proxy.yaml`) |
| `controller/controller.py` | Orchestration, reconcile loop, event dispatch |
| `controller/reconciler.py` | Model reconciliation |
| `controller/models.py` | `OicmModel` dataclass, `sanitize_model_id`, `detect_mode` |
| `controller/litellm_client.py` | LiteLLM REST client — **sends `LITELLM_ADMIN_KEY`** |
| `controller/controller.py` | **also hosts the inline health server** (`/health` on `HEALTH_PORT`, default 8090) — there is no separate `health.py` |
| `controller/sources/base.py` | `ModelSource` ABC |
| `controller/sources/local_deployments.py` | Watches `adeo` namespace Deployments |
| `controller/sources/submariner_imports.py` | Watches Submariner EndpointSlices |
| `controller/fallbacks/` | Fallback request client/service |
| `controller/pricing/` | Model pricing resolution (aggregator, matchers, resolver) |
| `controller/Dockerfile` | Container build |
| `controller/README.md` | Dev docs + **env var table (default derives from the manifest)** |

## Key entry points to edit

- **Add a new model source** → create a class implementing `ModelSource` in
  `controller/sources/` and register it.
- **Change how models reconcile** → `controller/reconciler.py`.
- **Change admin key handling** → `controller/config.py` (`LITELLM_ADMIN_KEY`)
  and `controller/litellm_client.py`.

## Deployment

Deployed via `deploy/prod/discovery-controller.yaml`, pinned to `adeo-gpu-03` (the
Submariner gateway node) with tolerations and RBAC for reading EndpointSlices.
The full env var table, build/push steps, and dependency graph live in
[Discovery Controller](../discovery-controller/README.md); cluster apply /
rollout steps live in [Deployment & Cluster](../deployment.md).

## Tests

- `tests/controller/test_reconciler.py`
- `tests/controller/pricing/*` — pricing tests (aggregator, matchers, resolver, source)

## Docs

- `docs/discovery-controller/README.md` — what it does, architecture, env vars, deployment
- `docs/discovery-controller/logic-map-and-code-smell-audit.md` — trace + audit
- `docs/model-pricing/PRICING-LOGIC-MAP.md` — pricing resolution logic
- `docs/architecture/IMPLEMENTATION_PLAN.md` — original design (component #1)