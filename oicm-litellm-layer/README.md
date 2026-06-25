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
│                           │   • custom_auth (component #2)    │  │
│                           │   • callbacks (component #3)      │  │
│                           │   • config.yaml (component #4)    │  │
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
| 2 | Custom Auth Handler | Plugin | `auth/oicm_auth.py` | Validate API keys against OICM `api_keys` table |
| 3 | VLLM Param Injector | Plugin | `hooks/vllm_param_injector.py` | Relocate vLLM-specific params into `extra_body` via `async_pre_call_hook` |
| 4 | KEDA Metrics Callback | Plugin | `hooks/keda_metrics.py` | Emit `ml_model_concurrent_requests` Prometheus gauge for KEDA |
| 5 | Config Template | Config | `config/litellm_config.yaml` | LiteLLM proxy configuration |
| 6 | Embedding Patch | Fork (5 lines) | — | Add `extra_body` merge to `hosted_vllm/embedding/transformation.py` |

## Quick Start

```bash
# 1. Build and push the sidecar image
docker build -t oicm-discovery-controller:latest ./controller

# 2. Deploy LiteLLM with the config
helm install litellm deploy/charts/litellm-helm/ \
  -f config/litellm-values.yaml

# 3. Deploy the discovery controller
kubectl apply -f deploy/discovery-controller.yaml

# 4. Apply the embedding patch (optional, for vLLM embedding extra_body)
cd /home/adeo/litellm
git apply ../oicm-litellm-layer/patches/embedding-extra-body.patch
```
