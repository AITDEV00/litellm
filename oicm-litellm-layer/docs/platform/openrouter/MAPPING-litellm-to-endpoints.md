# Mapping: litellm inputs to the `/endpoints` response

Reference for deciding what to fill next. Every field of
`GET /api/v1/models/{author}/{slug}/endpoints`, where its value comes from, and
what litellm data is currently unused.

Line refs are against commit `1a0b9d23c4`. Live values verified on dev
2026-10-07, the telemetry fields scraped after traffic warmed the 30m window.

The latency/throughput/uptime rows are populated by
`litellm/proxy/openrouter_compat/enrichment/telemetry.py`; the source decision
and the realtime-vs-historical accuracy audit are in
`MAPPING-usage-metrics.md`.

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
| `latency_last_30m` | p50/p75/p90/p99 of TTFT, ms | Prometheus TTFT histogram | §4b; `null` until a stream lands in the window |
| `throughput_last_30m` | p50/p75/p90/p99 of tokens/sec | Prometheus per-token latency histogram, inverted | §4b |
| `uptime_last_5m/30m/1d` | `success/(success+failure)*100` | Prometheus success/failure counters | §4b |
| `live_concurrency` | our extension | Prometheus in-progress gauge | §4b; int, peak replica |
| `requests_last_30m` | our extension | Prometheus total-requests counter | §4b |
| `perf_last_30m_by_workload` | absent | — | not set (needs a metric-to-workload classifier) |

## 4. Pricing precedence

`PricingResolver._resolve_deployment` (`pricing.py:47`), first hit wins:

1. `deployment.model_info["input_cost_per_token"]` + `["output_cost_per_token"]`
2. `litellm.get_model_info(upstream_id)` with the same two keys
3. `Pricing("0", "0")` (`unknown_policy="free"`)

Verified: `litellm.get_model_info("Qwen/Qwen3.6-35B-A3B-FP8")` **misses**, so for
a deployment without explicit cost keys the price is `0`, not a registry price.
The live non-zero prices come from `model_info` (which litellm populates from
`litellm_params` for a DB-registered model).

## 4b. Telemetry fields (latency / throughput / uptime)

Read by `litellm/proxy/openrouter_compat/enrichment/telemetry.py`
(`PrometheusDeploymentTelemetryReader`), keyed by `model_id`, from the cluster
Prometheus. All queries run concurrently, and the whole `model_id` map is cached
for 15s so a burst of `/endpoints` calls costs one query set per TTL.

| Field | Metric | Transform |
|---|---|---|
| `latency_last_30m` | `litellm_llm_api_time_to_first_token_metric` | `histogram_quantile` over `rate(...[30m])`, seconds to ms |
| `throughput_last_30m` | `litellm_deployment_latency_per_output_token` | `histogram_quantile` over `rate(...[30m])`, inverted to tokens/sec |
| `uptime_last_5m/30m/1d` | `litellm_deployment_success_responses_total` and `..._failure_responses_total` | `increase` per window, then `success/(success+failure)*100` |
| `live_concurrency` | `litellm_deployment_in_progress_requests` | `max by (model_id)`, truncated to int |
| `requests_last_30m` | `litellm_deployment_total_requests_total` | `increase` over 30m |

Semantics, all chosen to match OpenRouter's contract:

- Latency is **time to first token**, not total duration. The TTFT histogram is
  observed only for streamed requests, so a deployment with no recent stream
  reports `null`. This is honest, not a defect: at the moment of one dev check
  the TTFT count in the window was 1, so `histogram_quantile` could not resolve
  and the field was `null`; once a second sample landed it read
  `{p50: 38.4, p75: 45.1, p90: 49.2, p99: 92.7}`.
- Throughput is **per-request generation speed** (the inverse of the
  latency-per-output-token histogram), which is OpenRouter's definition. The
  counter-rate alternative (`rate(output_tokens)`) is a fleet capacity metric
  that under-reports speed under low load; see `MAPPING-usage-metrics.md` §2.4.
- Uptime is `success/(success+failure)*100` over the window. Success and failure
  are read as **two separate queries** rather than one ratio, because a
  deployment with zero failures has no failure series at all and a PromQL vector
  division against a missing operand yields an empty vector. A missing success
  series means no traffic, reported as `null`; a missing failure series defaults
  to 0, so a healthy deployment reports 100.
- All four percentiles are required. If any quantile is missing the whole
  distribution is reported as absent rather than fabricated from fewer points.

Two fields are **extensions beyond the OpenRouter contract**, on the same
`GatewayEndpoint` subclass as `gateway_status`: `live_concurrency` (the live
in-flight count) and `requests_last_30m`. OpenRouter has no top-level field for
either; it only exposes request volume nested per workload inside
`perf_last_30m_by_workload`, which we do not implement.

`live_concurrency` is the **peak in-flight count on the busiest replica** of the
deployment (`max by (model_id)`), not the fleet-wide sum. See §8g.

### Nullable fields serialize as `null`, not omitted

The SDK's `@model_serializer` keeps a nullable field when it is explicitly set,
so `latency_last_30m`, `throughput_last_30m`, the `uptime_*` fields,
`max_*_tokens`, and `quantization` all appear in the JSON as `null` when absent.
Only `perf_last_30m_by_workload` (optional, never set) is dropped. Verified on
live dev 2026-10-07.

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
those same unset fields. Confirmed by construction: the only non-test
`ModelLimits(...)` call is `discovery/adapters/openai_compatible.py:78`, which
sets `context_length` alone. They are not per-deployment values today, they are
structurally empty. Verified live: both serialize as `null`.

### 8d. `data.architecture` is hardcoded empty

`_response_architecture()` returns all-null. The real architecture
(`model_type`, `architectures`, `tokenizer`, `instruct_type`) is available in
`model.architecture` for SGLang deployments, and the modalities are in
`model.capabilities`, but the endpoints route emits neither. The list route does
map modalities.

### 8e. `perf_last_30m_by_workload` is not implemented

OpenRouter keys this by workload type (`text_generation`, `stt`, `tts`). We have
the per-deployment data to fill the `text_generation` bucket, but classifying a
metric series by workload needs a mapping that does not exist yet. Left absent
rather than fabricated.

### 8f. `live_concurrency` and `requests_last_30m` are extensions

Both sit on the `GatewayEndpoint` subclass alongside `gateway_status`. OpenRouter
has no top-level field for either, so a strict OpenRouter client ignores them.
They are the only two telemetry fields not in the official contract, and they are
documented as such at §4b.

### 8g. `live_concurrency` is the busiest replica's load

The query is `max by (model_id) (litellm_deployment_in_progress_requests)`. The
gauge carries one series per pod, so a `model_id` (one deployment) can have
several. The field reports the **peak in-flight count on the busiest replica**,
the load on that endpoint, not the fleet-wide sum across its pods.

This matches the reading `MAPPING-usage-metrics.md` §8f already called the honest
one for a per-endpoint (deployment) field. Summing would make a deployment spread
over four pods read four times busier than the same load on one pod, which is
fleet occupancy rather than per-endpoint load. A model-group level surface can
sum later if it needs the fleet view; this route is per-deployment, so it does
not.

`requests_last_30m` stays a sum: request counts add up across replicas, concurrent
occupancy does not.

## 9. What is still open

Recorded so these are not mistaken for finished. The full register, with
priorities, is in `docs/oicm-status/PROGRESS-AND-PAUSED-WORK.md`.

| Item | Where | Blocks |
|---|---|---|
| M2 engine-load telemetry (running/queued requests, KV utilization) | not started, no `RuntimeTelemetryProvider` in the tree | the instantaneous engine view; the reserved `-1` status depends on it |
| reserved `-1` endpoint status | `gateway_status.py`, deliberately unassigned | blocked on M2 |
| `perf_last_30m_by_workload` | not implemented (§8e) | needs a metric-to-workload classifier |
| runtime detection (§8a) | `registry.py::_detect_runtime_kind` | makes `provider_name`/`tag`/capabilities wrong for every deployment |
| discarded enrichment (§8b) | `models_service.py` | dead work per request |
| `max_*_tokens` always null (§8c) | `openai_compatible.py:78` | two always-null fields |
| `data.architecture` empty (§8d) | `_response_architecture()` | one always-empty envelope field |

Not open: M1 (Steps 13-18) and M3 Step 25, including all four telemetry fields
and their percentiles. `live_concurrency` semantics were pinned down on
2026-10-07 (§8g): it is the busiest replica's load, `max by (model_id)`.

## 10. Verified live (dev, 2026-10-07)

`GET /api/v1/models/zai-org/GLM-5.3/endpoints`, after six streaming requests
warmed the window:

```
latency_last_30m    : {p50: 37.7, p75: 44.1, p90: 47.9, p99: 68.0}     ms
throughput_last_30m : {p50: 400.0, p75: 266.7, p90: 222.2, p99: 202.0} tok/s
uptime_last_5m/30m/1d : 0.0 / 100.0 / 100.0
live_concurrency    : 0
requests_last_30m   : 11.35
status              : 0
max_prompt_tokens   : null (present, not omitted)
```

`live_concurrency` was also observed flipping `0 -> 1` mid-request, and the
windowed fields were observed reading `null` / `0.0` while the window held no
qualifying samples, then populating once it did. Those are the honest no-data
answers, not defects.

### The max-vs-sum change, verified on real multi-pod data

The semantics change from `sum by` to `max by` is measurable against prod, which
runs two pods for the same `model_id`. Read-only Prometheus queries, same instant:

```
raw gauge series for model_id f9a591e0 (GLM-5.3)
  pod litellm-proxy-5c46cf7f4c-79vdh -> 362
  pod litellm-proxy-5c46cf7f4c-vgnxz -> 354
sum by (model_id) -> 716    # old: double-counts a 2-pod deployment
max by (model_id) -> 362    # new: load on the busiest replica
```

On dev, with one replica, the field still tracks real in-flight work. A streaming
request that outlives a 30s scrape made the Prometheus gauge read `1`, and
`GET /api/v1/models/zai-org/GLM-5.3/endpoints` with a cold reader cache returned
`live_concurrency: 1` while the request was still in flight, then `0` once it
finished.
