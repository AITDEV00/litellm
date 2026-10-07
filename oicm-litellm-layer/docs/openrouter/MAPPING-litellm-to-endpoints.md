# Mapping: litellm inputs to the `/endpoints` response

Reference for deciding what to fill next. Every field of
`GET /api/v1/models/{author}/{slug}/endpoints`, where its value comes from, and
what litellm data is currently unused.

Line refs are against commit `95059edfe1`. Live values verified on dev
2026-10-07.

The `needs telemetry` rows (latency/throughput/uptime) are covered by
`MAPPING-usage-metrics.md`, which inventories all natively captured usage
data, verifies realtime-vs-historical accuracy on prod, and picks the
source per field.

## 1. The two names (read this first)

| Name | What it is | Where it comes from | Where it appears |
|---|---|---|---|
| routing name | the string you put in a request body | `deployment.model_name` | the URL match key, `endpoint.model_id`, `data.id` |
| served id | what the runtime reports at `/v1/models` | `deployment.litellm_params.model`, prefix stripped by the probe | `endpoint.model_name` |

They can differ (`Qwen/Qwen-Image-2.1` vs `Qwen-Image-2.1`) and litellm rejects
the served id, so the URL matches the routing name. See design §31.

## 2. Sources litellm gives us

| # | Source | Exact path | Read by |
|---|---|---|---|
| 1 | `get_available_models_for_user` | `resolver.py:59` | visibility gate; a model not in this set is not matchable |
| 2 | `llm_router.get_model_list()` | `resolver.py:90` | the deployment list |
| 3 | `deployment.model_name` | `resolver.py:110` | routing name |
| 4 | `deployment.model_info.id` | `resolver.py:110` | deployment id (health join key) |
| 5 | `deployment.litellm_params.api_base` | `resolver.py:103` | discovery target |
| 6 | `deployment.litellm_params.api_key` | `resolver.py:107` | discovery auth header |
| 7 | `deployment.litellm_params.custom_llm_provider` | `resolver.py:115` | runtime detection |
| 8 | `deployment.litellm_params.model` | `resolver.py:116` | cache key + runtime detection |
| 9 | `deployment.model_info` (dict) | `resolver.py:111` | `discovery_runtime` override, pricing |
| 10 | `litellm.get_model_info(upstream_id)` | `registry.py:20` | display name, hf id, capability fallback |
| 11 | `general_settings` | `models_service.py:67` | passed through, unused here |
| 12 | `prisma_client` | `models_service.py:101` | the health table |

Plus four HTTP probes against the deployment itself:

| Probe | Path | Fields |
|---|---|---|
| `openai_models` | `/v1/models` | `id`, `created`, `root`, `parent`, `max_model_len` |
| `sglang_model_info` | `/model_info` (fallback `/get_model_info`) | `is_generation`, `has_image_understanding`, `has_audio_understanding`, `model_type`, `architectures` |
| `openapi` | `/openapi.json` | 15 route-to-capability booleans + `routes` |
| — | — | |

## 3. Field-by-field: the endpoint object

One endpoint object per deployment.

| Response field | Value | Source | Notes |
|---|---|---|---|
| `model_id` | routing name | source 3 | the match key |
| `model_name` | served id, else routing name | probe 1 `id` | falls back at `endpoints.py:114` |
| `name` | `"{runtime_kind}: {model_name}"` | runtime kind | separator is `": "` |
| `tag` | runtime kind | `runtime.kind` | see §5 finding |
| `provider_name` | runtime kind | `runtime.kind` | see §5 finding |
| `context_length` | `deployment.limits.context_length or 0` | probe 1 `max_model_len` | **per deployment**, not aggregated |
| `max_prompt_tokens` | `deployment.limits.max_input_tokens` | never set | always `null` |
| `max_completion_tokens` | `deployment.limits.max_completion_tokens` | never set | always `null` |
| `pricing.prompt` / `.completion` | USD per 1M | `model_info` cost keys, else litellm registry, else `"0"` | see §4 |
| `status` | OpenRouter numeric | `GatewayStatus.endpoint_status()` | omitted when unmanaged/stale |
| `gateway_status` | our extension | health table | 8 fields, see §6 |
| `supported_parameters` | `[]` | — | **hardcoded empty**; the registry-backed resolver exists but is not called here |
| `supports_tool_choice` | all-false | — | hardcoded |
| `supports_implicit_caching` | `false` | — | hardcoded |
| `supports_image_reference` | `false` | — | hardcoded |
| `supports_multiple_audio_references` | `false` | — | hardcoded |
| `supports_voice_cloning` | `false` | — | hardcoded |
| `native_tools` | `{}` | — | hardcoded |
| `quantization` | `null` | — | hardcoded |
| `latency_last_30m` | `null` | — | needs telemetry |
| `throughput_last_30m` | `null` | — | needs telemetry |
| `uptime_last_5m/30m/1d` | `null` | — | needs telemetry |
| `perf_last_30m_by_workload` | absent | — | not set |

## 4. Pricing precedence

`PricingResolver._resolve_deployment` (`pricing.py:47`), first hit wins:

1. `deployment.model_info["input_cost_per_token"]` + `["output_cost_per_token"]`
2. `litellm.get_model_info(upstream_id)` with the same two keys
3. `Pricing("0", "0")` (`unknown_policy="free"`)

Verified: `litellm.get_model_info("Qwen/Qwen3.6-35B-A3B-FP8")` **misses**, so for
a deployment without explicit cost keys the price is `0`, not a registry price.
The live non-zero prices come from `model_info` (which litellm populates from
`litellm_params` for a DB-registered model).

## 5. The response envelope

| Field | Value | Source |
|---|---|---|
| `data.id` | canonical id | `canonical_id(f"{author}/{slug}")` |
| `data.name` | display name, else served id, else `public_id` | sources 10, probe 1 |
| `data.created` | `identity.created or 0` | probe 1 `created`, aggregated as first non-null |
| `data.description` | `"{public_id} served by the gateway across N deployment(s)."` | generated |
| `data.architecture` | all-null/empty | `_response_architecture()` hardcodes it |

## 6. `gateway_status`

| Field | Source |
|---|---|
| `oicm_status` | health `details["status"]`, verbatim |
| `availability` | derived from `oicm_status` + `details["serving_available"]`, or `unknown` when stale |
| `stale` | `now - heartbeat.checked_at > 90s`; `None` heartbeat means stale |
| `source` | `details["cluster"]` |
| `healthy` | the health row's own `status == "healthy"` |
| `replicas` | `details["replicas"]["desired"/"available"]` |
| `observed_at` | `details["observed_at"]` |
| `checked_at` | the `oicm-source-<cluster>` heartbeat row's `checked_at` |

## 7. Unused litellm data (candidates, not defects)

| Available | Read? | Note |
|---|---|---|
| `general_settings` | no | threaded through the whole call chain, never read |
| `litellm_params.model` | cache key only | the served id comes from the probe, not from here |
| `model_info.id` | health join only | |
| `model_info` capability keys (`supports_function_calling`, `supports_tool_choice`, `supports_reasoning`, `supports_response_schema`) | only via the list route | the enricher runs on the endpoints route but nothing reads its output |
| `model_info` context/limit keys | no | limits come from the probe |
| registry `supported_openai_params` | no (endpoints route) | resolver exists, unused here |
| `identity.hugging_face_id`, `identity.canonical_id`, `identity.root`, `identity.parent` | no (endpoints route) | list route only |

## 8. Findings that need a decision

### 8a. `tag` and `provider_name` are always `"openai-compatible"`

`_detect_runtime_kind` (`registry.py:54`) matches only the literal strings
`"sglang"` and `"vllm"`. A DB-registered model has
`litellm_params.model = "hosted_vllm/..."`, whose `custom_llm_provider` is
`"hosted_vllm"` (verified). That matches neither, so **every** deployment falls
through to the generic adapter.

Consequences, all live-confirmed:

- every endpoint reports `provider_name = "openai-compatible"` and
  `tag = "openai-compatible"`, so the runtime is not actually advertised
- the vLLM adapter is never selected, so `/openapi.json` is never probed and
  `api_capabilities` is never populated
- the SGLang adapter is never selected, so `/model_info` is never probed and
  `has_image_understanding` / `is_generation` are never read
- the only way to reach either richer adapter today is a per-deployment
  `model_info.discovery_runtime` override

### 8b. Capability enrichment runs but its result is discarded

`get_model_endpoints` (`models_service.py:129`) computes
`self._metadata_enricher.enrich(self._capability_enricher.enrich(page))`, but
`map_endpoints` reads only `deployment.limits`, `deployment.identity`,
`deployment.runtime`, `model.logical_model_name`, and `model.identity`. It never
reads `capabilities`. So both enrichers are dead work on this route. They are
live on the list route.

### 8c. `max_prompt_tokens` / `max_completion_tokens` are always `null`

Nothing populates `limits.max_input_tokens` or `limits.max_completion_tokens`:
the probe sets only `context_length`, and the aggregator would compute them from
those same unset fields. They are not per-deployment values today, they are
structurally empty.

### 8d. `data.architecture` is hardcoded empty

`_response_architecture()` returns all-null. The real architecture
(`model_type`, `architectures`, `tokenizer`, `instruct_type`) is available in
`model.architecture` for SGLang deployments, and the modalities are in
`model.capabilities`, but the endpoints route emits neither. The list route does
map modalities.
