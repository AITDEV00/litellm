# OICM → LiteLLM Integration Layer

An external sidecar that bridges the OICM model platform (Kubernetes) with LiteLLM proxy,
using only LiteLLM's public extension points — **no fork required** (except one 5-line embedding patch).

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        adeo namespace                           │
│                                                                 │
│  ┌──────────────┐         ┌──────────────────────────────────┐  │
│  │   K8s API    │  watch   │   oicm-discovery-controller     │  │
│  │  Deployments │◄────────│   (this repo, component #1)      │  │
│  │  j-{uuid}   │         │                                  │  │
│  └──────────────┘         │   on ADD:  POST /model/new       │  │
│                           │   on DEL:  POST /model/delete     │  │
│  ┌──────────────┐         │   on MOD:  POST /model/update     │  │
│  │  ConfigMaps  │────────►│                                  │  │
│  │  MODEL_ID    │  read   │   discovers MODEL_ID from:       │  │
│  └──────────────┘         │   1. ConfigMap MODEL_ID field     │  │
│                           │   2. Fallback: GET /v1/models     │  │
│  ┌──────────────┐         └──────────┬───────────────────────┘  │
│  │  Services    │                    │                          │
│  │  s-{uuid}   │                    │ REST API calls           │
│  │  :8080       │                    ▼                          │
│  └──────────────┘         ┌──────────────────────────────────┐  │
│                           │   LiteLLM Proxy                  │  │
│                           │   (unmodified, config-driven)     │  │
│                           │                                  │  │
│                           │   Extension points used:         │  │
│                           │   • callbacks (components #3, #4) │  │
│                           │   • config.yaml (component #5)    │  │
│                           │   • /model/new, /model/delete     │  │
│                           └──────────────────────────────────┘  │
│                                                                 │
│  ┌──────────────┐         ┌──────────────────────────────────┐  │
│  │   Redis      │◄───────│  Shared cache for multi-replica   │  │
│  │   (existing) │         └──────────────────────────────────┘  │
│  └──────────────┘                                               │
│                                                                 │
│  ┌──────────────┐         ┌──────────────────────────────────┐  │
│  │  PostgreSQL  │◄───────│  OICM api_keys table (read-only)  │  │
│  │  (existing)  │         │  LiteLLM Prisma tables (r/w)      │  │
│  └──────────────┘         └──────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Components

| # | Component | Type | File | Purpose |
|---|-----------|------|------|---------|
| 1 | Discovery Controller | K8s sidecar | `controller/` | Watch `j-{uuid}` deployments, register/deregister models via LiteLLM API |
| 3 | VLLM Param Injector | Plugin | `hooks/vllm_param_injector.py` | Relocate vLLM-specific params into `extra_body` via `async_pre_call_hook` (wired in the `litellm-hooks` ConfigMap) |
| 4 | KEDA Metrics Callback | Plugin | `hooks/keda_metrics.py` | Emit `ml_model_concurrent_requests` Prometheus gauge for KEDA (present in the repo, not currently registered) |
| 5 | Config Template | Config | `deploy/base/gateway/config/litellm-config.yaml` | LiteLLM proxy configuration (the deployed `litellm-config` ConfigMap) |

Component #2 (a `custom_auth` handler validating keys against OICM's `api_keys` table) and component #6 (the embedding `extra_body` patch) are both **not in use**. Auth is LiteLLM's native virtual-key auth, and the embedding patch is superseded by upstream (see `docs/components/patches.md`).

## Quick Start

```bash
# 1. Build and push the discovery controller image (versioned tag, see docs/Makefile-reference.md)
make controller-release

# 2. Apply the prod manifests (gateway overlay + controller + sources/exclusions)
make deploy

# 3. Or roll the gateway image and restart it in one step
make litellm-src-release

```

There is no Helm chart: everything is applied with `kubectl apply` / kustomize
from `deploy/` (see `docs/deployment.md`). For a dev iteration use
`make litellm-src-release-dev` and `make controller-deploy-dev`.

Note: the old embedding `extra_body` patch is no longer needed. Upstream
litellm now passes vLLM embedding `extra_body` params (e.g.
`truncate_prompt_tokens`) through natively, and the patch file no longer
applies. See `docs/components/patches.md`.

## Repository Layout

```
.
├── Makefile               build / push / deploy targets
├── pyproject.toml         Python project metadata (oicm-discovery entry point)
├── README.md              this file
├── CHANGELOG.md           release history
├── controller/            discovery controller source (component #1)
├── hooks/                 LiteLLM proxy plugins (components #3, #4)
├── deploy/                k8s manifests (discovery-controller, litellm-proxy, redis, ingress, postgres)
│   └── oicm/              OICM service-account provisioning Jobs + Secret templates
├── decor/                 UI assets (logos, favicon)
├── examples/usage/        runnable usage tests for gateway endpoints
│   ├── hamsa-stt/         HAMSA STT WebSocket test page
│   ├── image-generation/  Qwen-Image usage guide + gen_image.sh + test_image_endpoints.sh + inputs/
│   └── openrouter/        /api/v1/models demo
├── scripts/               helper scripts (htb_test, onboarding bundles, etc.)
├── benchmarks/            benchmark scripts (bench_2replicas, bench_after, bench_final, bench_minimax_vision)
├── tests/                 controller + hooks tests
└── docs/
    ├── admin-api/             LiteLLM proxy admin REST API reference
    ├── usage-guides/          how to call models through the gateway (see docs/docs-map.md)
    ├── custom-routes-plans/   custom-route logic map + VSA plan
    ├── htb-rate-limiting/     HTB priority-based rate limiting design and behaviour
    ├── incidents/             dated incident reports
    ├── performance/           gateway performance + OOM logic maps (before/after optimization)
    ├── dashboard-plan/        dashboard extension proposal (concurrency, top consumers)
    ├── techniques/            reusable analysis techniques (logic mapping, code smells)
    ├── runbooks/              operational runbooks (datasource validation, mkdocs setup)
    ├── cache-invalidation/    cache invalidation design + testing
    ├── architecture/          integration-layer implementation plan
    └── ...                    full map on docs/structure.md and docs/docs-map.md
```
