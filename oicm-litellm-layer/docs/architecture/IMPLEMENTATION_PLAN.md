# OICM → LiteLLM Integration Layer — Full Implementation Plan

## Executive Summary

This document provides a complete, build-ready implementation plan for the external integration layer between the OICM model platform and LiteLLM proxy. **No fork is required** (except one optional 5-line embedding patch). Every component uses LiteLLM's public extension points: REST APIs, `custom_auth`, `CustomLogger` callbacks, and `config.yaml`.

---

## Architecture Overview

```mermaid
graph TB
    subgraph "adeo namespace (model workloads)"
        D1[j-0826cff6<br/>Qwen3-Next-80B]
        D2[j-7c1848f6<br/>GLM-5.1-FP8]
        D3[j-d974abd1<br/>Qwen3.6-35B]
        D4[j-8ac37081<br/>Qwen3-Embedding-0.6B]
        D5[j-b10e5e96<br/>whisper-large-v3]
        D6[j-0bd81cf3<br/>XTTS-v2 ❌]
        S1[s-0826cff6:8080]
        S2[s-7c1848f6:8080]
        S3[s-d974abd1:8080]
        D1 --> S1
        D2 --> S2
        D3 --> S3
    end

    subgraph "mlops namespace (control plane)"
        K8S[Kubernetes API]
        CTRL[oicm-discovery-controller<br/>Component #1]
        LLM[LiteLLM Proxy<br/>unmodified]
        
        K8S -->|watch j-{uuid}| CTRL
        CTRL -->|POST /model/new<br/>POST /model/delete| LLM
        
        AUTH[custom_auth<br/>Component #2]
        HOOK[async_pre_call_hook<br/>Component #3]
        KEDA[keda_metrics callback<br/>Component #4]
        
        LLM --> AUTH
        LLM --> HOOK
        LLM --> KEDA
    end

    subgraph "Data stores"
        PG[(PostgreSQL<br/>mlops-postgres-rw)]
        RDB[(Redis<br/>redis-master)]
        PROM[(Prometheus<br/>kube-prometheus-stack)]
    end

    CTRL -.->|read OICM api_keys| PG
    AUTH -->|SHA-256 lookup| PG
    LLM -->|cache/rate-limit| RDB
    KEDA -->|ml_model_concurrent_requests| PROM
    PROM -->|KEDA triggers| D1
```

---

## Component #1: Discovery Controller

### What it does
Watches Kubernetes `Deployment` resources in the `adeo` namespace with label `oip/workload-type=model_deployment`, discovers each model's identity, and registers/deregisters them with LiteLLM via the `/model/new` and `/model/delete` REST APIs.

### K8s topology (discovered from kubectl)

Current state — **20 active model deployments**, all with label `oip/workload-type=model_deployment`:

| UUID | ConfigMap MODEL_ID | Service | Mode |
|------|-------------------|---------|------|
| `0826cff6` | `Qwen/Qwen3-Next-80B-A3B-Instruct` | `s-0826cff6:8080` | chat |
| `0bd81cf3` | `coqui/XTTS-v2` | `s-0bd81cf3:8080` | ⛔ TTS (skip) |
| `0f8674c2` | *(empty → MinerU via /v1/models)* | `s-0f8674c2:8080` | chat |
| `100af4eb` | `Qwen/Qwen3-Embedding-4B` | `s-100af4eb:8080` | embedding |
| `58e70e08` | *(empty → vibevoice via /v1/models)* | `s-58e70e08:8080` | ⛔ skip |
| `7c1848f6` | `zai-org/GLM-5.1-FP8` | `s-7c1848f6:8080` | chat |
| `7cfa13cc` | `Qwen/qwen3-embedding-0.6B` | `s-7cfa13cc:8080` | embedding |
| `8ac37081` | `Qwen/Qwen3-Embedding-0.6B` | `s-8ac37081:8080` | embedding |
| `9310947d` | *(empty → Qwen/Qwen3-ASR-1.7B)* | `s-9310947d:8080` | transcription |
| `9917a251` | `Qwen/Qwen3.5-122B-A10B-GPTQ-Int4` | `s-9917a251:8080` | chat |
| `9c57bce9` | *(empty → 405 on /v1/models)* | `s-9c57bce9:8080` | ⛔ non-OpenAI (skip) |
| `a500a62d` | `Qwen/Qwen3-Next-80B-A3B-Instruct` | `s-a500a62d:8080` | chat |
| `a5eea6ed` | *(empty → 405 on /v1/models)* | `s-a5eea6ed:8080` | ⛔ non-OpenAI (skip) |
| `b10e5e96` | `openai/whisper-large-v3` | `s-b10e5e96:8080` | transcription |
| `cd2850fc` | *(empty → 405 on /v1/models)* | `s-cd2850fc:8080` | ⛔ non-OpenAI (skip) |
| `d1b16562` | *(empty → diagnostics)* | `s-d1b16562:8080` | ⛔ skip |
| `d72f732b` | `openai/whisper-large-v3` | `s-d72f732b:8080` | transcription |
| `d974abd1` | `Qwen3.6-35B-A3B-FP8` | `s-d974abd1:8080` | chat (2 replicas) |
| `dbc727e2` | `Qwen/Qwen3.5-122B-A10B-GPTQ-Int4` | `s-dbc727e2:8080` | chat |
| `e31f77f6` | `Qwen/Qwen3.5-0.8B` | `s-e31f77f6:8080` | chat |

**Registerable models: ~14** (excluding 6 TTS/diagnostic/non-OpenAI)

### MODEL_ID discovery strategy

```
Priority 1:  ConfigMap configmap-{uuid}-main → data.MODEL_ID
             Works for: 0826cff6, 0bd81cf3, 100af4eb, 7c1848f6, 7cfa13cc, 
                        8ac37081, 9917a251, a500a62d, b10e5e96, d72f732b,
                        d974abd1, dbc727e2, e31f77f6
             
Priority 2:  GET http://s-{uuid}.adeo.svc.cluster.local:8080/v1/models
             Works for: 0f8674c2 → /app/MinerU2.5-2509-1.2B
                        9310947d → Qwen/Qwen3-ASR-1.7B
                        eb9de344 → /model/Qwen/Qwen3.5-35B-A3B-GPTQ-Int4
             
Fallback:    Use UUID as model_name (last resort)
```

### Model name sanitization

Path-style IDs from vLLM need sanitization:

| Raw vLLM model ID | Sanitized model_name |
|---|---|
| `Qwen/Qwen3-Next-80B-A3B-Instruct` | `Qwen/Qwen3-Next-80B-A3B-Instruct` |
| `/app/MinerU2.5-2509-1.2B` | `app--MinerU2.5-2509-1.2B` |
| `/model/Qwen/Qwen3.5-35B-A3B-GPTQ-Int4` | `model--Qwen--Qwen3.5-35B-GPTQ-Int4` |

### Duplicate model handling

Two pairs of deployments serve the **same model ID**:

| model_name | Deployments | Behavior |
|---|---|---|
| `Qwen/Qwen3-Next-80B-A3B-Instruct` | `0826cff6` + `a500a62d` | LiteLLM auto-load-balances |
| `openai/whisper-large-v3` | `b10e5e96` + `d72f732b` | LiteLLM auto-load-balances |
| `Qwen/Qwen3.5-122B-A10B-GPTQ-Int4` | `9917a251` + `dbc727e2` | LiteLLM auto-load-balances |

Both deployments are registered with the **same `model_name`** but **different `api_base`** values. LiteLLM's router treats them as a single model group and load-balances across them.

### API call: POST /model/new

The exact payload the controller sends for each deployment:

```json
{
  "model_name": "Qwen/Qwen3-Next-80B-A3B-Instruct",
  "litellm_params": {
    "model": "hosted_vllm/Qwen/Qwen3-Next-80B-A3B-Instruct",
    "api_base": "http://s-0826cff6.adeo.svc.cluster.local:8080",
    "api_key": "",
    "drop_params": true
  },
  "model_info": {
    "mode": "chat",
    "oicm_uuid": "0826cff6-db3f-499c-b889-ea4f5fc0dd04",
    "oicm_namespace": "adeo"
  }
}
```

**Key fields verified against source code:**
- `Deployment.model_name: str` — required (`types/router.py:436`)
- `Deployment.litellm_params.model: str` — required, only required field in litellm_params (`model_management_endpoints.py:1070`)
- `Deployment.litellm_params.api_base: Optional[str]` — accepts K8s service DNS (`types/router.py:329`)
- `Deployment.litellm_params.api_key: Optional[str]` — empty string works, vLLM defaults to "fake-api-key" (`types/router.py:328`)
- `Deployment.litellm_params.drop_params: Optional[bool]` — silently drops unsupported params (`types/router.py:349`)
- `Deployment.model_info.mode: Optional[Literal["embedding","chat","completion"]]` — tells LiteLLM which endpoint to route (`proxy/_types.py:941`)
- `Deployment.model_info` uses `ConfigDict(extra="allow")` — arbitrary metadata like `oicm_uuid` is accepted (`proxy/_types.py:963`)
- `STORE_MODEL_IN_DB=True` required — persists to `litellm_proxymodeltable`, hot-reloads router via `proxy_config.add_deployment()` (`model_management_endpoints.py:1143`)

### API call: POST /model/delete

```json
{
  "id": "<model_id returned by /model/new>"
}
```

**Verified:** `ModelInfoDelete.id: str` is the only field (`proxy/_types.py:935`). Deletes from DB + removes from router via `llm_router.delete_deployment(id=...)` (`model_management_endpoints.py:960`).

### RBAC requirements

The controller needs a ServiceAccount with a Role in the `adeo` namespace:

```yaml
rules:
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get", "list"]
  - apiGroups: [""]
    resources: ["configmaps"]
    verbs: ["get", "list"]
```

### File structure

```
controller/
├── __init__.py
├── discovery.py          # Main controller (K8s watch + LiteLLM API client)
└── Dockerfile
```

---

## Component #2: Custom Auth Handler

### What it does
Validates incoming API keys by looking them up in the OICM `api_keys` table (SHA-256 hash match). Returns a `UserAPIKeyAuth` with the key scoped to the specific model.

### OICM API key schema (from kubectl)

```sql
Table "public.api_keys"
  id                               | varchar(255)  | NOT NULL (PK)
  name                             | varchar(255)  | NOT NULL
  hash                             | varchar(255)  | NOT NULL  -- SHA-256 hex
  dotted                           | varchar(255)  | NOT NULL  -- e.g., "sk-s...5wn4"
  created_datetime_timestamp_ms    | bigint        | NOT NULL
  expiration_datetime_timestamp_ms | bigint        | (nullable)
  revoked                          | boolean       | NOT NULL DEFAULT false
  model_version_id                 | varchar(255)  | NOT NULL  -- UUID of the model
  namespace_id                     | varchar(255)  | NOT NULL  -- "adeo"
  author_id                        | varchar(255)  | NOT NULL
  tenant_id                        | varchar(255)  | DEFAULT ''
```

**Key stats:** 205 keys, 9 distinct users, 111 distinct key names, 0 revoked.

### Auth flow

```
1. Client:  POST /chat/completions  Authorization: Bearer sk-xxx...
2. LiteLLM: Extracts api_key = "sk-xxx..."
3. LiteLLM: Calls user_api_key_auth(request, api_key)  ← our handler
4. Handler: SHA-256("sk-xxx...") → hash_value
5. Handler: SELECT * FROM api_keys WHERE hash = hash_value
6. Handler: If found + not revoked → return UserAPIKeyAuth
7. LiteLLM: Uses UserAPIKeyAuth to enforce model scoping, RPM/TPM, etc.
```

### UserAPIKeyAuth mapping

| OICM field | UserAPIKeyAuth field | Purpose |
|---|---|---|
| `name` | `key_alias` | Display name in logs/UI |
| `model_version_id` → `model_name` | `models: [model_name]` | Scope key to specific model |
| `author_id` | `team_id` | Group keys by creator |
| `author_id` | `user_id` | Track who made the request |
| `expiration_datetime_timestamp_ms` | `expires` | Key expiration |
| `id` | `metadata.oicm_key_id` | Traceability |
| `namespace_id` | `metadata.oicm_namespace` | Traceability |
| `dotted` | `metadata.oicm_dotted` | Log-friendly obfuscated key |

### Extension point verified

**File:** `litellm/proxy/proxy_server.py:4372`
```python
custom_auth = general_settings.get("custom_auth", None)
if custom_auth is not None:
    user_custom_auth = get_instance_fn(value=custom_auth, config_file_path=config_file_path)
```

**File:** `litellm/proxy/auth/user_api_key_auth.py:1076`
```python
response = await user_custom_auth(request=request, api_key=api_key)
validated = UserAPIKeyAuth.model_validate(response)
```

**Config:** `general_settings.custom_auth: auth.oicm_auth.user_api_key_auth`

The `get_instance_fn` loader resolves dotted module paths relative to the config file directory. The auth module lives outside the LiteLLM package.

### File structure

```
auth/
├── __init__.py
└── oicm_auth.py    # Custom auth handler
```

---

## Component #3: VLLM Param Injector

### What it does
Intercepts requests to `hosted_vllm/*` models and relocates vLLM-specific params (guided_json, thinking_token_budget, etc.) from the top-level `data` dict into `data["extra_body"]`. This bypasses LiteLLM's param validation while ensuring the params reach vLLM in the HTTP body.

### Why this works without forking

The openai_like chat handler builds the final HTTP body as:

**File:** `litellm/llms/openai_like/chat/handler.py:278`
```python
data = {"model": model, "messages": messages, **optional_params, **extra_body}
```

The `**extra_body` spread merges all extra_body keys into the HTTP body, bypassing param validation. Our hook moves vLLM params into `extra_body` before this point.

### Params relocated

| Param | vLLM docs | Currently in LiteLLM? | After hook? |
|---|---|---|---|
| `guided_json` | Structured output (JSON schema) | ❌ Not supported | ✅ Via extra_body |
| `guided_regex` | Structured output (regex) | ❌ Not supported | ✅ Via extra_body |
| `guided_choice` | Structured output (choice list) | ❌ Not supported | ✅ Via extra_body |
| `guided_grammar` | Structured output (Lark grammar) | ❌ Not supported | ✅ Via extra_body |
| `thinking_token_budget` | Integer thinking token budget | ❌ Not supported | ✅ Via extra_body |
| `reasoning_parser` | Reasoning content parser | ❌ Not supported | ✅ Via extra_body |
| `chat_template` | Custom chat template | ❌ Not supported | ✅ Via extra_body |
| `lora_name` | LoRA adapter name | ❌ Not supported | ✅ Via extra_body |

### Extension point verified

**File:** `litellm/integrations/custom_logger.py:69`
```python
async def async_pre_call_hook(
    self,
    user_api_key_dict: UserAPIKeyAuth,
    cache: DualCache,
    data: dict,
    call_type: CallTypesLiteral,
) -> Optional[Union[Exception, str, dict]]:
```

**File:** `litellm/proxy/utils.py:1498` — the proxy calls this hook and replaces `data` with the returned dict.

**Config:** `litellm_settings.callbacks: hooks.vllm_param_injector.VllmParamInjector`

### Limitation: embedding requests

The embedding handler (`hosted_vllm/embedding/transformation.py`) does NOT merge `extra_body` into the HTTP request body. This hook cannot help for embeddings. See the 5-line fork patch in `patches/embedding-extra-body.patch`.

### File structure

```
hooks/
├── __init__.py
└── vllm_param_injector.py    # async_pre_call_hook callback
```

---

## Component #4: KEDA Metrics Callback

### What it does
Emits the `ml_model_concurrent_requests{model_id="<uuid>"}` Prometheus gauge that the existing KEDA ScaledObject uses for autoscaling.

### Current KEDA setup

Only 1 model has KEDA autoscaling: `0f8674c2` (MinerU)

```yaml
# From: kubectl get scaledobject -n adeo -o yaml
spec:
  scaleTargetRef:
    kind: Deployment
    name: j-0f8674c2-5fb8-43c7-bf2d-a6e5db2c0ff4
  triggers:
    - type: prometheus
      metadata:
        metricName: ml_model_concurrent_requests
        query: sum(ml_model_concurrent_requests{model_id="0f8674c2-5fb8-43c7-bf2d-a6e5db2c0ff4"})
        serverAddress: http://kube-prometheus-stack-prometheus.kube-prometheus-stack.svc.cluster.local:9090
        threshold: "24"
```

Our callback emits the **exact same metric name and label** so this ScaledObject continues to work without modification.

### Extension point verified

**File:** `litellm/integrations/custom_logger.py`
- `async_log_pre_api_call(self, model, messages, kwargs)` — called before API call
- `async_log_success_event(self, kwargs, response_obj, start_time, end_time)` — called on success
- `async_log_failure_event(self, kwargs, response_obj, start_time, end_time)` — called on failure

All are OSS. The Prometheus gauge is registered with `prometheus_client.Gauge` and appears on the same `/metrics` endpoint that LiteLLM's built-in `PrometheusLogger` mounts.

**Config:** `litellm_settings.callbacks: hooks.keda_metrics.KEDAMetricsCallback`

### File structure

```
hooks/
├── __init__.py
├── vllm_param_injector.py
└── keda_metrics.py    # Prometheus gauge for KEDA
```

---

## Component #5: Config Template

### What it does
The `litellm_config.yaml` that wires everything together. Mounted into the LiteLLM proxy pod.

### Key settings

| Setting | Value | Why |
|---|---|---|
| `store_model_in_db: true` | Required for `/model/new` | Dynamic model registration |
| `custom_auth: auth.oicm_auth.user_api_key_auth` | OICM key validation | No LiteLLM key migration needed |
| `callbacks: [VllmParamInjector, KEDAMetricsCallback]` | Plugin registration | vLLM params + KEDA metrics |
| `drop_params: true` | Silently drop unsupported params | Prevents errors on vLLM-specific params |
| `success_callback: ["prometheus"]` | Mount /metrics endpoint | Required for KEDA + observability |
| `pass_through_endpoints` | Non-OpenAI model proxy | TTS models that can't use hosted_vllm |

### File structure

```
config/
└── litellm_config.yaml
```

---

## Component #6: Embedding Patch (Optional Fork)

### What it does
Adds `extra_body` merge support to the hosted_vllm embedding handler. Without this, vLLM-specific embedding params (like `truncate_prompt_tokens`) are silently dropped.

### The patch (5 lines)

In `litellm/llms/hosted_vllm/embedding/transformation.py`, after `map_openai_params`:

```python
# Merge extra_body for vLLM-specific embedding params
extra_body = non_default_params.pop("extra_body", None)
if extra_body and isinstance(extra_body, dict):
    optional_params.update(extra_body)
```

### File structure

```
patches/
└── embedding-extra-body.patch
```

---

## Deployment

### K8s manifests

```
deploy/
├── discovery-controller.yaml   # Controller Deployment + RBAC
└── litellm-proxy.yaml          # LiteLLM Proxy Deployment + ConfigMap
```

### Prerequisites

1. **PostgreSQL**: The OICM postgres at `mlops-postgres-rw.mlops.svc.cluster.local:5432` is used by both:
   - LiteLLM's Prisma client (needs a separate database or schema — `DATABASE_URL` can point to a different DB on the same server)
   - The custom auth handler (reads from `oicm.api_keys` table)

2. **Redis**: Existing Redis at `redis-master.redis.svc.cluster.local:6379` for multi-replica caching

3. **Secrets**:
   - `litellm-admin-key` — master key for LiteLLM admin API
   - `litellm-secrets` — DATABASE_URL, OICM_DB_URL, master-key

4. **RBAC**: The discovery controller needs read access to deployments, services, and configmaps in the `adeo` namespace

### Migration path

1. **Phase 1**: Deploy LiteLLM alongside the existing OICM gateway (both running)
2. **Phase 2**: Deploy the discovery controller → models appear in LiteLLM
3. **Phase 3**: Switch DNS/ingress from OICM gateway to LiteLLM proxy
4. **Phase 4**: Decommission the OICM gateway

---

## Complete File Tree

```
oicm-litellm-layer/
├── README.md                          # Architecture overview
├── IMPLEMENTATION_PLAN.md             # This document
├── setup.py                           # Package setup
├── requirements.txt                   # Dependencies
│
├── controller/
│   ├── __init__.py
│   └── discovery.py                   # Component #1: K8s watch → LiteLLM API
│
├── auth/
│   ├── __init__.py
│   └── oicm_auth.py                   # Component #2: OICM key validation
│
├── hooks/
│   ├── __init__.py
│   ├── vllm_param_injector.py         # Component #3: vLLM param passthrough
│   └── keda_metrics.py                # Component #4: KEDA Prometheus gauge
│
├── config/
│   └── litellm_config.yaml            # Component #5: LiteLLM proxy config
│
├── patches/
│   └── embedding-extra-body.patch     # Component #6: 5-line embedding fix
│
└── deploy/
    ├── discovery-controller.yaml      # K8s manifests for controller
    └── litellm-proxy.yaml             # K8s manifests for LiteLLM proxy
```

---

## Build Order & Effort Estimate

| Step | Component | Effort | Dependencies |
|------|-----------|--------|-------------|
| 1 | Config template (#5) | 30 min | None |
| 2 | Custom auth (#2) | 2 hours | Config, OICM DB access |
| 3 | Param injector (#3) | 1 hour | Config |
| 4 | KEDA callback (#4) | 1 hour | Config, Prometheus |
| 5 | Discovery controller (#1) | 4 hours | Config, LiteLLM API |
| 6 | Embedding patch (#6) | 15 min | LiteLLM source |
| 7 | Integration testing | 4 hours | All components |
| 8 | Deployment manifests | 1 hour | All components |
| **Total** | | **~14 hours** | |

---

## Testing Strategy

### Unit tests
- `test_discovery.py` — mock K8s API, verify MODEL_ID discovery and sanitization
- `test_oicm_auth.py` — mock postgres, verify SHA-256 lookup and UserAPIKeyAuth mapping
- `test_vllm_param_injector.py` — verify param relocation to extra_body
- `test_keda_metrics.py` — verify gauge increment/decrement

### Integration tests
1. Start LiteLLM with config pointing to a test postgres
2. Run discovery controller against a mock K8s API server
3. Verify models appear in `GET /model/info`
4. Send a chat completion with `guided_json` → verify it reaches the vLLM backend
5. Send a request with an OICM key → verify auth succeeds
6. Check `/metrics` for `ml_model_concurrent_requests` gauge

### End-to-end test
1. Deploy to mlops namespace alongside the existing OICM gateway
2. Register a test model via the discovery controller
3. Send requests through LiteLLM and verify they reach the correct vLLM pod
4. Compare responses with the OICM gateway for parity
