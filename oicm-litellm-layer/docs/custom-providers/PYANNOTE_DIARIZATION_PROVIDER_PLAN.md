# pyannote Diarization Provider: Choices and Implementation Plan

> **Status**: plan (2026-10-19). Records the design decisions for exposing a
> coro-served `POST /v1/diarize` through the OICM LiteLLM gateway as a
> `pyannote` provider, and the vertical-slice implementation plan across the
> litellm tree and the discovery controller.
>
> **Revision 2 (same day)**: coro now supports a configurable serving model id
> (commit `99633ba`, `CORO_PYANNOTE_DIARIZE_MODEL_ID`), and the ADEO image
> bakes `pyannote/community-1` (commit `8ddf659`), so the gateway, the pod,
> and the client all agree on one id end to end. This removes the asymmetry
> Revision 1 left (gateway `pyannote/community-1` vs pod `community-1`) and
> with it the reliance on the first-slash split landing exactly on the pod's
> enum value. Two mechanical consequences are recorded below: `model_name`
> becomes `pyannote/community-1` (no longer the bare enum value), and the
> gateway must always populate the `model` form field (the pod's hardcoded
> Form default `community-1` is rejected on the renamed deployment).
>
> **Revision 1 (same day)**: corrected the model id to the API enum value
> `community-1` (the `DiarizeRequest.model` enum is `precision-2`,
> `community-1`, `precision-3`). The earliest draft used coro's Hugging Face
> pipeline id `pyannote/speaker-diarization-community-1`, which is an
> engine-internal model-selection id, not an API model id. Provider detection
> moved to the OpenAPI path probe as a consequence.

## Context

A coro deployment (the ASR/diarization engine, `/home/jyao/DGE/coro`) serves a
pyannoteAI-exact `POST /v1/diarize` plus `GET /v1/jobs/{jobId}`. The contract is
pinned by coro's conformance suite against the official
`docs.pyannote.ai/openapi.json` fixture, so the pod side of the contract cannot
drift. The gateway must make that endpoint reachable under LiteLLM virtual-key
auth, routing, and spend logging, selectable by model id, with the pod address
discovered dynamically by the OICM controller (no static `api_base`).

The H200 MIG gateway plan (coro ADR 0025) routes `/v1/audio/transcriptions`
through LiteLLM to per-workload coro deployments "so the deployment must be
selectable by model id". Diarization is the same requirement one surface over.

## Recorded choices

### C1. Provider name: `pyannote`, not `coro`; one id `pyannote/community-1` end to end

Coro is a tool wrapper: the contract the gateway forwards to is pyannoteAI's
`DiarizeRequest`/`DiarizationJob`/`DiarizationJobOutput`/`ValidationErrorResponse`
shapes, and any future non-coro engine that serves the same contract (including
pyannoteAI's own cloud) must be interchangeable behind the same provider. The
provider is the contract; the engine is an implementation detail.

The serving model id is settings-driven on the pod (coro commit `99633ba`,
`ServerSettings.pyannote_diarize_model_id`, env
`CORO_PYANNOTE_DIARIZE_MODEL_ID`): the pyannoteAI spec enum value
`community-1` stays the source default so the cloud contract holds for bare
runs, and the ADEO image bakes
`CORO_PYANNOTE_DIARIZE_MODEL_ID=pyannote/community-1` (commit `8ddf659`) for
gateway deployments. The served weights never change; the id is the
request/response-facing name only.

With the rename, one id serves all three roles and the earlier asymmetry is
gone:

| role | id | why |
|---|---|---|
| client sends / `model_name` / spend-log `model` | `pyannote/community-1` | matches the pod's accepted id, so what the client asks for is what the log says and what the pod echoes |
| registered `litellm_params.model` | `pyannote/pyannote/community-1` | controller payload is `{provider}/{model_id}`; the double prefix is what makes the first-slash split yield the pod's accepted id |
| pod accepts and echoes (`CORO_PYANNOTE_DIARIZE_MODEL_ID`) | `pyannote/community-1` | baked in the ADEO image |

The double-prefix `pyannote/pyannote/community-1` is not a mistake: the
controller's `_register_payload` composes `model = f"{provider}/{model_id}"`,
and `get_llm_provider` strips exactly one leading `pyannote/` before the SDK
call, so the pod receives `pyannote/community-1` verbatim. The client never
types the double prefix; it types `model_name`. (`community-1` as
`model_name` would also work — the split would yield bare `community-1`,
which bare coro runs accept but the renamed ADEO deployment rejects — but
then the spend log and the UI would disagree with what the pod echoes. One
id everywhere is the whole point.)

`pyannote/speaker-diarization-community-1` (coro's internal Hugging Face
pipeline id, one layer below the API contract) appears nowhere in the
gateway.

Consequences:

- `LlmProviders.PYANNOTE = "pyannote"` in `litellm/types/utils.py`
- Provider dir `litellm/llms/pyannote/`
- The `model` form field is **required at the gateway**: coro's route keeps a
  hardcoded `Form(default="community-1")` even when the accepted id is
  renamed, so an omitted field is the spec-shaped 400 on the ADEO deployment
  (verified at coro HEAD). The gateway route defaults `data["model"]` to the
  deployment's `model_name` when the client omits it, so the common curl
  (`-F file=@meeting.wav -F exclusive=true`) works, and the config forwards
  the field verbatim to the pod
- Because the id now contains the `pyannote` prefix, provider detection has a
  secondary signal: `KNOWN_PROVIDERS` matching on the id works again
  (`"pyannote" in "pyannote/community-1"`). The OpenAPI path probe
  (`/v1/diarize` in paths) remains the primary signal since it does not
  depend on the ConfigMap id being set correctly (see Slice G)

### C2. Full 7-layer vertical slice, not pass-through

`pass_through_endpoints` targets a static URL and cannot resolve the
controller-discovered pod `api_base`, so the route gets the full treatment:
proxy route slice -> `route_llm_request` dispatch -> router methods -> SDK
functions -> HTTP handler -> `litellm/llms/pyannote/` config. This is the same
call the VSA-PLAN made for the OmniVoice routes; the reasoning carries over
verbatim.

### C3. Multipart in, JSON out; jobs and GET-poll out of scope for slice 1

Coro's transport delta from the cloud contract is multipart `file` + form
fields instead of JSON body with `url` (fetching arbitrary URLs from a private
cluster is an SSRF hole; the cloud requires a URL because it is multi-tenant).
LiteLLM already parses multipart at Layer 1 (`get_form_data`), and the handler
already forwards `form_data` + `files` (voice-clone path). The config builds
the multipart POST with `files={"file": (...)}` and every form field passed
through verbatim. No OpenAI param mapping is attempted: the diarize surface is
not an OpenAI surface, and inventing one would extend a contract a third party
owns, which is the exact error coro ADR 0015 rejects.

`GET /v1/jobs/{jobId}` is **deliberately out of scope for slice 1**: coro's
POST returns the completed job inline (synchronous engine, hundreds-x real
time), so a poll loop is a compatibility nicety, and coro keeps only the last
128 jobs in memory, so the gateway must not advertise durable retrieval. The
route can be added later without touching the existing layers (new dispatch
key + GET route in the slice).

### C4. Unsupported-field 400s pass through verbatim

Coro rejects 14 `DiarizeRequest` fields with the spec's own
`ValidationErrorResponse` shape, per field. The gateway does not intercept,
translate, or pre-validate those: the 400 body is the vendor contract, and
clients written against pyannoteAI's documented behavior read it as-is. The
config declares no `get_supported_openai_params` mapping beyond the
pass-through of form fields; `drop_params` does not apply because there is no
OpenAI parameter vocabulary on this surface.

### C5. New mode value: `diarization`

`model_info.mode` today carries `chat`/`embedding`/`completion`/
`image_generation`/`audio_transcription`/`rerank`/`moderations` (and
`audio_speech` via the controller's `to_litellm_mode`). Diarization is not
transcription: coro's diarize returns speaker turns only, and mode drives both
UI grouping and the controller's non-chat routing decisions. Register
`mode: "diarization"`:

- `litellm/types/utils.py`: add `"diarization"` to the `ModelInfo.mode`
  Literal (the one in the model-prices schema block) and to
  `supported_endpoints` vocabulary if the dashboard's mode filter reads from
  there
- `litellm/proxy/_types.py` `ModelInfo`: widen its
  `Literal["embedding", "chat", "completion"]` to include `"diarization"`
  (this is the class `Deployment.model_info` validates through
  `litellm/types/router.py`; check both Literals before relying on the value)
- Controller `_PATH_MODES`: add `("/v1/diarize", "diarization")` **before**
  the chat guard, following the ordering rule in `detect_mode_from_paths`
  (most specific first; a chat-capable server that also exposes diarize stays
  chat). Coro's OpenAPI at `/openapi.json` does advertise `/v1/diarize`, so
  the probe path already works.

The mode string is cosmetic for routing (dispatch is by route, not mode), but
it keeps the Admin UI model list honest and gives the controller a value its
tests can assert on.

### C6. Auth: no header injection

Coro ships no authentication by design (ADR 0013 accepts the lint finding).
`validate_environment` returns headers unchanged (Content-Type set by the
multipart encoder). LiteLLM virtual-key auth sits in front at the proxy layer,
which is the whole point of the gateway. No `PYANNOTE_API_KEY` env plumbing.

### C7. Cost: no pricing in slice 1

Diarization has no token vocabulary. Pricing, if ever needed, is per-second of
audio, which is a `provider_specific_entry` concern in the pricing resolver
(`controller/pricing/`), not a token-price entry. Defer; log spend rows with
zero cost and a follow-up ticket if billing matters. (coro's `/v1/listen` and
`/v1/audio/transcriptions` surfaces keep their existing paths; this slice
touches only `/v1/diarize`.)

### C8. Slice placement per `oicm-slices.md`

- Routes: new sibling slice `litellm/proxy/diarization_routes.py` (NOT inline
  in `proxy_server.py`), mounted next to the existing OICM router mounts at
  the bottom of `proxy_server.py` (after `client_distribution_router`), plus
  two thin mount lines
- SDK functions: `litellm/endpoints/diarization/` mirroring
  `litellm/endpoints/voice/` (`__init__.py` re-export + `main.py`),
  re-exported lazily via `litellm/__init__.py.__getattr__` so
  `litellm.diarize` resolves
- Provider config: `litellm/llms/pyannote/{common_utils.py,diarization/}`
  following the hamsa/omnivoice dir shape
- Registry: `litellm/llms/oicm_providers/registry.py` gains
  `get_provider_diarization_config`; `ProviderConfigManager` in `litellm/utils.py`
  delegates with one clean line
- Drop-detection: extend
  `tests/test_litellm/proxy/test_oicm_drop_detection.py` with
  `test_diarization_routes_router_is_mounted_on_app` and
  `test_diarization_sdk_slice_wired_into_litellm_namespace`

### C9. Response passthrough, not typed models

The gateway returns the pod's `DiarizationJob` JSON as-is (minus hop-by-hop
headers), same as `create_voice` returns the pod's JSON dict. Do not build
Pydantic response models in the gateway: the contract's single source of truth
is the pinned spec fixture in coro's tree, and duplicating it here would
violate the DRY rule the coro ADRs are built on. Response models, if ever
needed for the dashboard's generated types, are generated from the proxy's
own OpenAPI export, which derives from the route's declared response shape.

## Implementation plan

### Slice A: types + registration (no runtime behavior)

1. `litellm/types/utils.py`: `LlmProviders.PYANNOTE = "pyannote"`; add
   `"diarization"` to the `ModelInfoBase.mode` Literal and to
   `CallTypes`/`CallTypesLiteral` (`adiarize`/`diarize`)
2. `litellm/proxy/_types.py`: widen the `ModelInfo.mode` Literal
3. `litellm/constants.py`: add `/v1/diarize` to
   `API_ROUTE_TO_CALL_TYPES` mapped to the new call types (both with and
   without the `/v1` prefix, matching how `/v1/audio/speech` is double-listed)
4. `litellm/utils.py`: add `get_provider_diarization_config` to
   `ProviderConfigManager`, delegating to the registry slice
5. `litellm/llms/oicm_providers/registry.py`: add
   `get_provider_diarization_config(provider)` returning
   `PyannoteDiarizationConfig()` for `PYANNOTE`
6. Tests: registry dispatch test in
   `tests/test_litellm/llms/oicm_providers/test_oicm_provider_registry.py`

### Slice B: provider config (Layer 6)

```
litellm/llms/pyannote/
├── __init__.py
├── common_utils.py          ← PyannoteModelInfo(BaseLLMModelInfo)
└── diarization/
    ├── __init__.py
    └── transformation.py   ← PyannoteDiarizationConfig
```

`PyannoteModelInfo`: `get_models` returns `("pyannote/community-1",)`, the
serving id the ADEO deployment accepts (the spec enum value `community-1` is
the bare-run default and is rejected by the renamed deployment;
`precision-2`/`precision-3` are cloud-only, the pod rejects them, so the
gateway does not advertise them); `get_api_base` reads `PYANNOTE_API_BASE`
(dev convenience only; prod comes from the controller's `litellm_params`).
`get_provider_info` returns
`ProviderSpecificModelInfo(endpoint="/v1/diarize", mode="diarization")`.

`PyannoteDiarizationConfig(BaseDiarizationConfig)` - new base class, see
Slice C. Methods:

- `get_complete_url`: `f"{api_base}/v1/diarize"`. The controller registers the
  bare ClusterIP for native-base providers; decide whether `pyannote` joins
  `NATIVE_BASE_PROVIDERS` (bare base, config appends `/v1/diarize`) or keeps
  the `/v1` suffix convention. **Decision: join `NATIVE_BASE_PROVIDERS`** -
  coro serves other native surfaces (`/v1/listen`, WebSocket) from the same
  pod, so the bare base is the honest registration
- `validate_environment`: no-op header return (C6)
- `transform_diarization_request`: builds multipart. Inputs: the parsed form
  fields dict + the `(filename, bytes, content_type)` file tuple. Output:
  `DiarizationRequestData(form_data=form_fields_minus_file, files={"file": file_tuple})`.
  Every form field passes through verbatim, including `model`,
  `exclusive`, and the 14 unsupported fields (their 400s come back from the
  pod, C4)
- `transform_diarization_response`: parses the JSON body and returns it as a
  dict (C9); attaches `_hidden_params` from response headers where the
  success-path header builder expects them

### Slice C: new handler entry point (Layer 5)

Add `diarization_handler`/`async_diarization_handler` to
`litellm/llms/custom_httpx/llm_http_handler.py` next to `voice_handler`,
because no existing handler takes `(form_data, files)` in and returns JSON
out: the TTS handler returns binary, the voice handler takes JSON in. The
shape mirrors `voice_handler`: `validate_environment` ->
`get_complete_url` -> `transform_diarization_request` ->
`async_httpx_client.post(url, data=..., files=...)` ->
`transform_diarization_response`.

New base class `BaseDiarizationConfig` at
`litellm/llms/base_llm/diarization/transformation.py` mirroring
`BaseAudioTranscriptionConfig`'s abstract-method set, plus
`DiarizationRequestData` TypedDict (`form_data: dict[str, str]`,
`files: dict[str, tuple[str, bytes, str]]`). The multipart-heavy shape is why
diarization is not squeezed into `BaseAudioTranscriptionConfig`:
transcription's request data carries a single audio file and OpenAI-mapped
optional params, while diarize forwards a whole form surface verbatim.

### Slice D: SDK functions (Layer 4)

`litellm/endpoints/diarization/main.py`:

- `adiarize(*args, **kwargs)`: executor wrapper exactly like `acreate_voice`
- `@client def diarize(model, file, exclusive=None, custom_llm_provider=None,
  api_base=None, ...)`: `get_llm_provider` -> `get_litellm_params` ->
  `ProviderConfigManager.get_provider_diarization_config` ->
  `diarization_handler(...)`. The `file` parameter is the
  `(filename, bytes, content_type)` tuple (the route already normalized the
  upload to that shape at Layer 1)
- `__init__.py` re-exports `adiarize`/`diarize`;
  `litellm/__init__.py.__getattr__` gains the two names

### Slice E: router methods (Layer 3)

`litellm/router.py` next to `acreate_voice`/`_acreate_voice`:
`adiarize()` (public, fallbacks) and `_adiarize()` (deployment resolution +
`litellm.adiarize(**data)`), borrowing `_update_kwargs_before_fallbacks`,
`async_get_available_deployment`, `_update_kwargs_with_deployment`.

### Slice F: dispatch + route (Layers 1-2)

`litellm/proxy/route_llm_request.py`: add `"adiarize"` to the `route_type`
Literal and `"adiarize": "/v1/diarize"` to `ROUTE_ENDPOINT_MAPPING`.

`litellm/proxy/diarization_routes.py` (new slice):

- `@router.post("/v1/diarize")` + `@router.post("/diarize")` (double-listing
  matches every other audio route)
- Handler mirrors `audio_speech_clone`'s multipart flow: `get_form_data`,
  strip `file`, `add_litellm_data_to_request`, model resolution
  (`data["model"]` from the form, defaulted to the deployment's
  `model_name` (`pyannote/community-1`) when the client omits it — required,
  because the pod's hardcoded `community-1` Form default is rejected on the
  renamed ADEO deployment), `pre_call_hook(call_type="adiarize")`,
  `route_request(route_type="adiarize")`,
  custom headers via `ProxyBaseLLMRequestProcessing.get_custom_headers`,
  return `JSONResponse` of the pod body (C9)
- Mount in `proxy_server.py` next to the other OICM slice mounts:
  `from litellm.proxy.diarization_routes import router as
  oicm_diarization_router` + `app.include_router(oicm_diarization_router)`
- Route allowlist: `gateway/routes/allowlist.py` and
  `backend/routes/allowlist.py` both list routes (e.g. `/openapi.json`); check
  whether the hardened surface gates audio routes and add `/v1/diarize` if so

### Slice G: discovery controller

Facts that drive the changes (verified against coro's served surface):

- coro publishes `/openapi.json` (ADR 0013) whose paths include
  `/v1/diarize`, `/v1/audio/transcriptions`, `/v1/listen`, `/v1/jobs/{jobId}`
  and `/health` - so the controller's `_probe_openapi_paths` works unchanged
- coro serves **no** `/v1/models`, so `_discover_model_ids` has no
  `owned_by` source: the deployment ConfigMap `MODEL_ID` is the model-id
  source. The coro workload's ConfigMap must set
  `MODEL_ID=pyannote/community-1` (the same string the pod's
  `CORO_PYANNOTE_DIARIZE_MODEL_ID` is baked to, so the registered
  `model_name`, the pod's accepted id, and what the pod echoes all agree)
- the model id (`pyannote/community-1`) now contains the `pyannote`
  substring, so `KNOWN_PROVIDERS` matching the id works as a secondary
  signal; the OpenAPI path probe stays primary since it does not depend on
  the ConfigMap id being set

`oicm-litellm-layer/controller/models.py`:

1. `detect_provider`: add a path-based branch after the `KNOWN_PROVIDERS`
   loop, mirroring the paddlex branch: `if "/v1/diarize" in paths and
   CHAT_PATH not in paths: return "pyannote"`. Also add `"pyannote"` to
   `KNOWN_PROVIDERS` — with the serving id `pyannote/community-1` the
   id-substring match fires, making the ConfigMap id a redundant but
   harmless secondary signal; the path probe remains primary because it
   holds even when `MODEL_ID` is misconfigured
2. `NATIVE_BASE_PROVIDERS`: add `"pyannote"` (C8/B decision above)
3. `_PATH_MODES`: add `("/v1/diarize", "diarization")` **before**
   `TRANSCRIPTION_PATH`. Order matters here as nowhere else: a coro pod
   serves both transcriptions and diarize, and the table returns its first
   hit, so diarize-first classifies coro pods as `diarization`. Pods serving
   transcriptions without diarize (vLLM ASR, vibevoice) are unaffected, and
   chat-capable servers never reach the table (chat guard)
4. `_NAME_MODES` name fallback: add `("diariz", "diarization")` token match
   for probe-failure fallback
5. A coro deployment also serves `/v1/audio/transcriptions`; exposing that
   surface through the gateway is a separate slice (the OpenAI-compatible
   route, not this one) and is deliberately out of scope here

`oicm-litellm-layer/controller/litellm_client.py` `_register_payload` needs no
change: it already stamps `provider/model_id`
(`pyannote/pyannote/community-1`, the double prefix being what makes the
first-slash split land on the pod's accepted id) and `mode` through. Verify
the payload's `model_info.mode="diarization"` passes the Deployment
validation (Slice A widening makes it legal).

Controller tests: extend `tests/controller/test_models.py` with the new
detection rows (path-based pyannote detection from a coro-shaped path set;
diarize-before-transcription ordering; the `("diariz", ...)` name fallback;
`pyannote/community-1` register payload), and `test_local_deployments.py`
with a coro-shaped openapi.json probe fixture (paths including `/v1/diarize`,
`/v1/audio/transcriptions`, `/health`; no `/v1/models`).

### Slice H: docs + usage guide

- This document (plan/choices) in `custom-providers/`
- `usage-guides/PYANNOTE_DIARIZATION_GUIDE.md` with curl examples against the
  live gateway (multipart file upload, `exclusive=true`, the per-field 400
  behavior)
- `mkdocs.yml` nav + `docs-map.md` entries for both
- `custom-providers/README.md` provider table gains a pyannote row

### Implementation order and verification

1. **Slice A** (types + registration) - compile-only, run registry tests
2. **Slice B+C** (config + handler) - unit tests with mocked httpx asserting
   the multipart POST shape (form fields verbatim, `files={"file": ...}`, URL
   `{api_base}/v1/diarize`)
3. **Slice D+E** (SDK + router) - unit tests asserting dispatch to the
   handler for `pyannote/...` models
4. **Slice F** (dispatch + route + mount) - drop-detection tests green; curl
   the live proxy with `dev_config.yaml` + a manual
   `pyannote/pyannote/community-1` deployment entry pointing at a running
   coro pod, showing the pod's `DiarizationJob` body round-tripped verbatim
   with the served id `pyannote/community-1` in the response (both command
   and output recorded per the repo's proof-of-fix convention)
5. **Slice G** (controller) - controller test suite; on-cluster: coro
   deployment registered automatically, `model_name` and `mode` verified in
   `/v1/model/info`
6. **Slice H** (docs) - `make docs` builds clean

Mutation-testing standard per repo CLAUDE.md applies to each slice's tests:
the registry dispatch test must fail when the `pyannote` branch is removed, the
multipart test must fail when a form field is dropped, and the drop-detection
tests must fail when the router mount or lazy re-export is deleted.

## Risks / open questions

- **`model` form field**: the gateway defaults it to the deployment's
  `model_name` when the client omits it, so the common curl works; but it is
  NOT optional at the pod on the ADEO deployment. Coro's route keeps the
  hardcoded `Form(default="community-1")` while the accepted id is
  settings-driven (verified at coro HEAD): the default and the accepted id
  disagree once `CORO_PYANNOTE_DIARIZE_MODEL_ID=pyannote/community-1` is
  baked, so a request that lets the default through gets the spec-shaped
  per-field 400 naming the accepted id. The gateway defaulting the field is
  what shields clients from that; a coro follow-up could make the Form
  default track the setting, but the gateway must not depend on it.
  A client explicitly sending `model=precision-2` or `model=community-1`
  gets the pod's own per-field 400, which is the vendor contract behaving
- **Timeout**: coro diarizes at hundreds-x real time; a 2h meeting is ~3-5s,
  but gateway timeout should default generously (e.g. 600s) and be
  per-deployment overridable via `litellm_params.timeout`
- **`excluded`-field drift**: if pyannoteAI's spec adds a `DiarizeRequest`
  field, coro's conformance suite fails first (upstream pin refresh), and the
  gateway passes the new field through verbatim (no gateway change needed) -
  the layering means spec drift is a coro concern, not a gateway one
- **Streaming/long-audio**: out of scope; coro's sync job returns inline and
  the pod holds no durable queue
