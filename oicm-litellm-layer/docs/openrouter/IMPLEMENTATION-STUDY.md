# OpenRouter-Compatible Model Discovery — Implementation Study

**Status:** Implementation study / scratch pad  
**Design source:** [litellm_openrouter_models_design.md](litellm_openrouter_models_design.md)  
**Target branch:** `jya0-v1.96.2` (fork of BerriAI/litellm)  
**Date:** 2026-08-14

This document studies how each phase in the design doc can be implemented against the
current LiteLLM codebase and the OICM integration layer. It records the concrete files,
types, functions, and runtime behaviors that each phase must build on. It is a working
scratch pad: phase-by-phase analysis, call-site evidence, decisions, and risks. Where a
phase requires a code change, this document is the study; the actual implementation
landing in `litellm/proxy/openrouter_compat/` will follow this.

---

## 1. Scope recap (what we are implementing)

A new, separately-routed endpoint:

```http
GET /api/v1/models
```

That:

- returns the OpenRouter Models API list shape (`data`, `total_count`, `links.next`);
- discovers live deployment facts from the actual upstream runtime (SGLang / vLLM) via
  `api_base` derived from LiteLLM deployments;
- reuses LiteLLM auth + model-access filtering, the router deployment registry, and the
  LiteLLM pricing/cost map;
- never couples internal logic to the OpenRouter SDK schema (only the mapper layer does).

Secondary route (must be live, not a dead `links.details` URL):

```http
GET /api/v1/models/{author}/{slug}/endpoints
```

---

## 2. Key codebase facts discovered during study

### 2.1 Existing OpenAI-compatible `/v1/models`

- `litellm/proxy/proxy_server.py:8789` — `model_list()` handler.
  Decorated with `@router.get("/v1/models", dependencies=[Depends(user_api_key_auth)], tags=["model management"])`
  and also `/models`.
- It calls `get_available_models_for_user(...)` from `litellm/proxy/utils.py:6366` and
  `create_model_info_response(...)` from `litellm/proxy/utils.py:6463`.
- Auth dependency is `user_api_key_auth` from `litellm/proxy/auth/user_api_key_auth.py`.
- Visibility filtering: blocked + unhealthy model names are hidden (lines ~8810-8820).

**Implication for design §2.1:** `/v1/models` stays as-is. We add `/api/v1/models` as a
brand-new APIRouter; no conflict with the existing route because the path prefix differs.

### 2.2 Model-access resolution (`get_available_models_for_user`)

Signature (from `litellm/proxy/utils.py:6366`):

```python
async def get_available_models_for_user(
    user_api_key_dict, llm_router, general_settings, user_model,
    prisma_client, proxy_logging_obj, team_id,
    include_model_access_groups, only_model_access_groups,
    return_wildcard_routes, user_api_key_cache,
) -> list[str]
```

- Builds key models (`get_key_models`), team models (`user_api_key_dict.team_models`,
  `get_team_models`), then unions via `get_complete_model_list(...)` from
  `litellm/proxy/auth/model_checks.py`.
- `scope=expand` admin path bypasses filtering and returns `llm_router.get_model_names()`.

**Design §15 "Filter inaccessible logical models before discovery fan-out"** maps directly
to reusing `get_available_models_for_user` (or the `scope=expand` branch) to get the
caller-visible logical model names **before** any upstream probe. This is exactly the
security invariant: a model the caller cannot invoke must not appear.

### 2.3 Router → deployment resolution

- `Router.get_model_names()` (`router.py:9831`) — all visible logical model names.
- `Router.get_model_list(model_name)` (`router.py:9999`) — returns
  `list[DeploymentTypedDict]` for one logical group (includes aliases + wildcard routes).
- `Router._get_all_deployments(model_name, team_id)` (`router.py:9757`) — O(1) index lookup
  of concrete deployments.
- `DeploymentTypedDict` = `{model_name, litellm_params, model_info}`.
- `Deployment` (`litellm/types/router.py:448`) — `model_name`, `litellm_params`, `model_info`.
- `LiteLLM_Params` carries `api_base`, `model`, `custom_llm_provider`, `api_key`,
  `input_cost_per_token`, `output_cost_per_token`, and (via `ConfigDict(extra="allow")`)
  arbitrary runtime hint fields.
- `ModelInfo` (`litellm/types/router.py:136`) — per-deployment metadata (`id`, `team_id`,
  `base_model`, `blocked`, and `extra="allow"` for arbitrary discovery hints).

**Discovery target = `litellm_params.api_base`.** This is exactly what the discovery
controller sets when registering a model. In the OICM layer the controller builds
`api_base` as `http://s-{uuid}.{ns}.{domain}:8080/v1` (see `controller/models.py`). So for
runtime probing we probe `api_base + /models` (or `/v1/models` if the base lacks `/v1`).

### 2.4 Pricing / cost map reuse (design §14)

- `litellm.litellm_core_utils.get_model_cost_map.get_model_cost_map(url)` and
  `litellm.model_cost` (the loaded map dict).
- `litellm.get_model_info(model)` returns a `ModelInfo`-like dict with
  `input_cost_per_token`, `output_cost_per_token`, `max_input_tokens`, `max_output_tokens`,
  `mode`, `supported_openai_params`, `supports_*` flags.
- `Router.get_model_group_info(model_group)` (`router.py:9323`) returns a `ModelGroupInfo`
  (from `litellm/types/router.py`) with `input_cost_per_token`, `output_cost_per_token`,
  `max_input_tokens`, `max_output_tokens`, `mode`, `supports_vision`, etc. This is the
  single best existing "registry lookup" for a logical model.
- `create_model_info_response(...)` (`utils.py:6463`) shows the canonical pattern for
  resolving configured token limits via `llm_router.get_configured_token_limits(model_id)`.

**Pricing enrichment layer** should resolve via `litellm.get_model_info(logical_name)` and
fall back to per-deployment `model_info`/`litellm_params` overrides, mirroring
`get_deployment_model_info` (`router.py:9210`) precedence. Note the design's strict
"live context beats theoretical registry" rule: we must never let `max_input_tokens` from
the cost map overwrite a live `max_model_len` from the runtime.

### 2.5 Route registration (design §4)

- `proxy_server.py` builds `app = FastAPI(...)` at line ~1246, then includes routers at
  lines 17258-17305 (`app.include_router(...)`).
- New packages follow the pattern of e.g. `litellm/proxy/health_endpoints/_health_endpoints.py`
  (module-level `router = APIRouter()`, decorated handlers, exported via `__init__`).
- Discovery endpoints pattern: `litellm/proxy/discovery_endpoints/ui_discovery_endpoints.py`
  exports `router` from `__init__.py`.

**Plan:** create `litellm/proxy/openrouter_compat/routes/` with
`models.py` and `model_endpoints.py`, each exposing `router = APIRouter()`. Add one
`include_router` line in `proxy_server.py`. Keep all business logic out of the route file
(delegating to `service.py`).

### 2.6 Auth dependency

- `user_api_key_auth` from `litellm.proxy.auth.user_api_key_auth` is the standard
  dependency returning `UserAPIKeyAuth`.
- Reuse it exactly as `/v1/models` does: `dependencies=[Depends(user_api_key_auth)]`.

### 2.7 Async HTTP (design §17)

- `litellm/llms/custom_httpx/http_handler.py` provides `AsyncHTTPHandler` (class at line
  504) — a shared async httpx client with connection pooling, TLS config helpers
  (`get_ssl_verify`, `get_ssl_configuration`). This is the project-standard client; the
  design's `DiscoveryHTTPClient` should wrap or reuse this rather than roll its own.

### 2.8 OpenRouter SDK availability

- Official `openrouter` PyPI package (v1.1.54 as of 2026-08-14) exposes
  `openrouter.components.model.Model`. Not currently installed in the workspace; must be
  pinned in the image/build per design §2.3, §39.
- The generated `Model` class is a Pydantic model; contract tests will validate our mapper
  output by constructing an `OpenRouterModel`.
- The current live OpenRouter list response shape (captured from the public
  `/api/v1/models`) is:

```json
{
  "data": [{
    "id": "...", "canonical_slug": "...", "hugging_face_id": null,
    "name": "...", "created": 1688256000, "description": "...",
    "context_length": 8192,
    "architecture": {"modality": "text->text", "input_modalities": ["text"],
                     "output_modalities": ["text"], "tokenizer": "...", "instruct_type": null},
    "pricing": {"prompt": "0.00000006", "completion": "0.00000006"},
    "top_provider": {"context_length": 8192, "max_completion_tokens": 4096, "is_moderated": false},
    "per_request_limits": null, "supported_parameters": [...],
    "default_parameters": {}, "supported_voices": null,
    "knowledge_cutoff": null, "expiration_date": null,
    "links": {"details": "/api/v1/models/{author}/{slug}/endpoints"},
    "benchmarks": {...}, "reasoning": {...}
  }],
  "total_count": 411,
  "links": {"next": null}
}
```

### 2.9 The OICM deployment reality (from the admin API + controller)

- The OICM discovery controller registers models dynamically via `POST /model/new` with
  `litellm_params = {model, api_base, api_key, drop_params, ...}` plus optional pricing
  (`input_cost_per_token`, `output_cost_per_token`) from `controller/pricing/`.
- The upstream runtime is reached at ClusterIP `s-{uuid}.{ns}.svc.cluster.local:8080`.
- Runtimes are primarily vLLM and SGLang; `detect_mode_from_paths` in `controller/models.py`
  already detects chat/embedding/rerank/transcription/tts from runtime paths. Our discovery
  adapters duplicate a safer subset of this (runtime detection), but the source of truth for
  runtime kind should be `litellm_params` metadata / explicit override (`discovery_runtime`),
  since we can't always probe.

---

## 3. Phase-by-phase implementation study

### Phase 0 — Baseline

**Goal:** pin the OpenRouter SDK and add an import/contract smoke test.

**Findings / decisions:**
- Install `openrouter` (pin e.g. `openrouter==1.1.54`) into the litellm image and the dev
  `pyproject.toml` used by `make bootstrap`.
- Add `requirements-openrouter.txt` (or add to existing deps) so it is present at test time.
- The SDK requires Python >=3.10; repo already targets >=3.8 in places but proxy runs modern
  Python, so this is fine. Verify the pinned version with the actual build.
- Add `tests/test_litellm/test_openrouter_compat/test_smoke.py` that does:
  `from openrouter.components.model import Model as OpenRouterModel` and asserts it imports.
- Capture current SGLang/vLLM fixtures (DTO-level, see Phase 2). This must be done against a
  real or recorded upstream; the existing OICM deployments are the reference.

**Exit criteria:** `pytest tests/test_litellm/test_openrouter_compat/test_smoke.py` passes
with the pinned SDK; LiteLLM tests still import.

### Phase 1 — Canonical domain

**Goal:** pure domain types with no runtime/OpenRouter imports.

**Plan** (`litellm/proxy/openrouter_compat/domain/`):
- `identity.py` → `ModelIdentity` (from design §5.1).
- `limits.py` → `ModelLimits` (context_length, max_input_tokens, max_completion_tokens).
- `architecture.py` → `ModelArchitecture` (model_type, architectures, instruct_type).
- `capabilities.py` → `ModelCapabilities` (tri-state `bool | None` for input/output
  modalities), `ApiCapabilities` (routes set + chat/embedding/transcription flags).
- `deployment.py` → `DiscoveredDeploymentModel` (identity + limits + architecture +
  capabilities + api + runtime + provenance).
- `logical_model.py` → `AggregatedModel`.
- `provenance.py` → `FactSource`, `ModelProvenance`.

**Rules enforced here:** no `openrouter`, `sglang`, or `vllm` imports. Pydantic `BaseModel`
with `slots`-style frozen dataclasses where possible (repo convention: composition over
inheritance, no mutation, fully typed).

### Phase 2 — Runtime DTOs

**Plan** (`discovery/dto.py` or `transport/dto.py`):
- `UpstreamDTO(BaseModel)` with `ConfigDict(extra="allow")`.
- `OpenAICompatibleModelCard`, `RuntimeModelCard` (adds `max_model_len`),
  `VLLMModelCard` (adds `permission`), `SGLangModelCard`, `SGLangModelInfo`
  (adds `has_image_understanding`, `has_audio_understanding`, `model_type`,
  `architectures`, `model_path`).

**Fixture tests** (`tests/test_litellm/test_openrouter_compat/test_dto.py`):
- known fields parse, extra fields don't fail, missing optionals don't fail.

### Phase 3 — Shared transport

**Design:** reuse `litellm.llms.custom_httpx.http_handler.AsyncHTTPHandler` rather than a
new client. Wrap it in a small `DiscoveryHTTPClient` that:
- `async def get_json(deployment, path, *, timeout)` with a default timeout (1-3s).
- resolves auth headers from `litellm_params.api_key` (reuse existing credential helpers).
- enforces a response-size cap (esp. for `/openapi.json`).
- categorizes errors: `DiscoveryTimeout`, `DiscoveryConnectionError`,
  `DiscoveryHTTPError`, `DiscoveryInvalidJSON`.
- never logs secrets.

**Errors** in `transport/errors.py`.

### Phase 4 — Reusable probes

**Design** (`discovery/probes/`):
- `OpenAIModelsProbe` — `GET /v1/models` (or `api_base` + models path).
- `SGLangModelInfoProbe` — `GET /model_info`, fallback `GET /get_model_info`.
- `OpenAPISchemaProbe` — `GET /openapi.json` (optional; tolerate 404/disabled).
- `OpenAPIInspector` — `has_path`, `http_methods`, `schema_has_field`, `parameter_names`
  (design §12).
- `ProbeResult` carries success/failure/data/source/latency/error_category so an optional
  probe failure never fails the catalog.

### Phase 5 — Adapter framework

**Design** (`discovery/adapters/base.py`, `registry.py`):
- `BaseDiscoveryAdapter(ABC)` → `RuntimeDiscoveryAdapter` → `OpenAICompatibleRuntimeAdapter`
  → `VLLMDiscoveryAdapter` / `SGLangDiscoveryAdapter`.
- `AdapterRegistry` selects by `litellm_params.discovery_runtime` override, else falls back
  to generic OpenAI-compatible.

### Phase 6 — vLLM adapter

- Probe `/v1/models` (+ optional `/openapi.json`).
- Map `id→identity.upstream_model_id`, `created→identity.created`, `max_model_len→limits.context_length`,
  `root→identity.root` (internal only), `owned_by→runtime hint`.
- `permission` is not modality metadata.

### Phase 7 — SGLang adapter

- Probe `/v1/models`, `/model_info` (fallback `/get_model_info`), optional `/openapi.json`.
- `has_image_understanding`→ image modality; `has_audio_understanding`→ audio modality;
  `model_type`→ architecture.model_type; `architectures`→ architecture.architectures.

### Phase 8 — LiteLLM deployment resolver

**Design** (`discovery/resolver.py`):
- `LiteLLMDeploymentResolver.resolve_for_request(auth_context)` → returns
  `list[DeploymentDescriptor]` where each descriptor wraps
  `{deployment_id, model_name, litellm_params, model_info}`.
- Filter logical models with `get_available_models_for_user` first (design §15) → we only
  fan out discovery for models the caller can invoke.
- Deduplicate identical `api_base`+runtime+auth targets (design §18) so one `/openapi.json`
  fetch is reusable per request.

### Phase 9 — LiteLLM enrichment / pricing

**Design** (`enrichment/pricing.py`):
- `PricingResolver.resolve(logical_model, deployments)` → `{prompt, completion}` in
  OpenRouter string-per-token format.
- Source precedence (design §13):
  1. explicit deployment pricing/config
  2. `litellm.get_model_info` cost map
  3. fallback/unknown policy (never silently zero unless zero is the billing reality).
- Enrich missing capabilities from the cost map but never overwrite live limits.

### Phase 10 — Aggregator

**Design** (`aggregation/aggregator.py`, `policies.py`):
- Group discovered deployments by logical `model_name`.
- `ContextAggregationPolicy` default `GUARANTEED_MIN`; `MAX_AVAILABLE` configurable.
- Completion limit: conservative min when multiple explicit limits.
- Modality/capability: advertise only capabilities guaranteed across all routable
  deployments (capability-aware routing caveat documented).
- Unknown vs unsupported handled per design §22; configurable strict mode can omit a model.

### Phase 11 — OpenRouter mapper

**Design** (`mapping/openrouter.py`):
- `OpenRouterModelMapper.map_model(AggregatedModel) -> OpenRouterModel`.
- The ONLY module importing `openrouter.components.model.Model` (and sibling generated
  types).
- Mapping for: id (= logical model_name, per §27), canonical_slug (e.g. `litellm/<name>`),
  name (§28 priority), created (deterministic, not faked), pricing, context_length,
  architecture (modality + input/output arrays with the design's required-field policy),
  top_provider (guaranteed context/completion; `is_moderated` default policy), supported/
  default parameters, supported_voices, links.details.
- Contract test (`tests/.../test_openrouter_contract.py`) imports the pinned class and
  validates `isinstance(result, OpenRouterModel)` and that serialized JSON preserves
  nullable required fields.

### Phase 12 — Routes

**Design** (`routes/models.py` and `routes/model_endpoints.py`):
- `@router.get("/api/v1/models", dependencies=[Depends(user_api_key_auth)])`
- `@router.get("/api/v1/models/{author}/{slug}/endpoints", dependencies=[...])`
- Handler calls a thin `service.py` orchestration; returns `{data, total_count, links}`
  (and for the endpoints route, per-deployment details using `AggregatedModel.deployments`).
- Register via `app.include_router(openrouter_models_router)` in `proxy_server.py`.

---

## 4. Concrete call sites to hook into

| Concern | File | Symbol / approach |
|---|---|---|
| Auth dep | `litellm/proxy/auth/user_api_key_auth.py` | `user_api_key_auth` |
| Model access filter | `litellm/proxy/utils.py` | `get_available_models_for_user` |
| Router logical names | `litellm/router.py` | `get_model_names`, `get_model_list`, `_get_all_deployments` |
| Deployment metadata | `litellm/types/router.py` | `Deployment`, `LiteLLM_Params.api_base` |
| Pricing map | `litellm/litellm_core_utils/get_model_cost_map.py` | `get_model_cost_map`, `litellm.model_cost` |
| Model info lookup | `litellm/utils.py` | `litellm.get_model_info` |
| Model group info | `litellm/router.py` | `get_model_group_info` |
| Async HTTP | `litellm/llms/custom_httpx/http_handler.py` | `AsyncHTTPHandler` |
| Route registration | `litellm/proxy/proxy_server.py` | `app.include_router` |
| Runtime kind override | `LiteLLM_Params` extra field | `discovery_runtime` |

---

## 5. Open questions / risks

- **Runtime kind detection.** The OICM controller currently does not stamp
  `discovery_runtime` on the deployment. For V1 we can infer from `/v1/models` `owned_by`
  / `max_model_len` presence, but explicit override is more robust. Recommend controller
  stamping `litellm_params.model_info.discovery_runtime` on registration (a small change in
  `controller/litellm_client.py`).
- **`api_base` trailing `/v1`.** Some registered bases include `/v1` (`...:8080/v1`), some
  not. The probe must normalize the path (strip a trailing `/v1` before appending the probe
  path).
- **Live probing cost/latency.** LiteLLM already had expensive `/v1/models` model-info
  paths. Bounded concurrency + optional TTL cache (design §34) must be wired from the
  start; measure at 10/25/100 deployments.
- **OpenRouter SDK required vs nullable fields.** When OpenRouter bumps a required field,
  contract tests fail until a policy is added; that is desired per design §2.4.

---

## 6. OICM-specific integration notes

- The discovery controller already reads `/v1/models` for MODEL_ID fallback and the model
  deployment paths; our new probes are a read-only, per-request fan-out over the same
  `api_base` values the controller configured. No persistent store is introduced.
- The controller's pricing resolver (`controller/pricing/`) sets per-deployment
  `input_cost_per_token`/`output_cost_per_token`. Our pricing resolver reads those same
  deployment-level values first, keeping one source of truth.
- Security: probes only target trusted `litellm_params.api_base` from LiteLLM config. No
  query/body/model-ID/user-header-driven targets (design §16). The OICM cluster IPs
  (ClusterIP/globalnet) stay internal; never surfaced in the OpenRouter output.

---

## 7. Test plan (mapped to design §37)

- DTO fixtures (SGLang/vLLM cards) — `test_dto.py`.
- Probe behavior (200/malformed/401/404/500/timeout; model_info fallback; openapi disabled)
  — `test_probes.py`.
- vLLM / SGLang adapter mapping — `test_adapters.py`.
- Aggregation policies (single/multi/different contexts/modalities/partial-fail/all-fail)
  — `test_aggregation.py`.
- Pricing precedence — `test_pricing.py`.
- OpenRouter contract (`isinstance(OpenRouterModel)`) — `test_openrouter_contract.py`.
- Endpoint integration with mock upstreams (auth filtering, parallel discovery, partial
  failure, no internal paths leak, pagination) — `test_endpoints.py`.
- Security (no injected URLs, no credential leak, no unlimited concurrency) — `test_security.py`.
- Performance benchmark (10/25/100) — `test_performance.py`.

---

## 8. Build / dependency maintenance

Per design §39:
1. pin LiteLLM fork deps
2. install pinned `openrouter` SDK
3. run unit + contract tests
4. build immutable image
5. deploy