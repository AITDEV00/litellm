# Nemotron Native Audio: Detailed Implementation Plan

> **Status**: implementation plan, 2026-11-20
>
> **Target deployment**: NVIDIA NIM `nemotron-transcribe-1.0.0-amd64`, serving
> `nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B`
>
> **Objective**: expose the NIM's native transcription, translation, response,
> and offline-file streaming behavior through ordinary LiteLLM endpoints while
> retaining virtual-key auth, dynamic deployment resolution, guardrails,
> logging, cost accounting, default JSON clients, and native response metadata
>
> **Terminology**: when this plan says *native field preservation*, it means a
> normal provider config maps multipart request fields and parses response
> content. It never means a LiteLLM pass-through endpoint, a provider URL
> relay, or use of `allm_passthrough_route`

## 1. Hard constraints and scope

Do not add, extend, configure, or invoke:

- `/passthrough` or `pass_through_endpoints`
- `/nvidia_nim/{model-group}/...` or any model-group URL relay
- `allm_passthrough_route`
- `BasePassthroughConfig` or `nvidia_nim/passthrough/`
- raw socket/WebSocket relay. Parsed provider streams must be governed typed events, not `body_iterator` byte forwarding

The existing audio-in-chat and chat SSE route remains untouched. The planned
offline multipart streaming is separate: the entire audio file is uploaded
first, then the provider emits an incremental transcript, and the gateway
walks down the SSE response one event at a time. It does not stream input
microphone audio and it does not create a realtime API

Do not create a `nvidia_nemotron` provider. The public model is a checkpoint
id, not a protocol. Also do not classify every NVIDIA-owned model as NIM:
the flexible `hosted_vllm` provider correctly covers OpenAI-compatible vLLM
and sglang servers

### 1.1 User-visible acceptance targets

| Behavior | Current gateway behavior | Required final behavior |
| --- | --- | --- |
| JSON multipart transcription with fields including `diarization=true` | Extra fields lost at different points, `diarization` refused or dropped by hosted-vLLM | Native request option names and values reach NIM through `/v1/audio/transcriptions` |
| Rich optional response fields | First response may contain native fields, identical cached response narrows them | Same public JSON schema on cold and cached responses |
| `response_format=text` | JSON parsing error, or JSON wrapper for other provider paths | 200 `text/plain`; body is the transcript, not `{"text": "..."}` |
| `response_format=srt`, `vtt` | NIM response fails JSON parsing | SRT or VTT document with its actual media type |
| `stream=true` multipart input | Complete response buffered and converted to JSON/invalid UTF-8 parsing failures | Gateway returns `text/event-stream`, delta arrives before completion, native done metadata retained |
| `/v1/audio/translations` | Not mounted | First-class authenticated endpoint using dynamic router deployments |
| Audio-in-chat with `input_audio` and `stream=true` | Works through audio mode plus explicit model calls | Byte/shape-compatible chat behavior, not rewritten by this plan |

The verified native verbose response preserved fields including
`schema_version`, `word_timestamp_alignment`, `transcript_origin`,
`diarization_frame_seconds`, `diarization_timestamps`,
`speaker_word_timestamps`, `subtitle_cues`, and `words`. These are examples
of fields that must survive as data, not a list the gateway should maintain
in its own schema

## 2. Proven architecture to follow

The normal OpenAI-shaped stack remains:

```text
HTTP /v1/audio/transcriptions
  -> auth + multipart parsing + guardrails
  -> route_request("atranscription")
  -> Router.atranscription() resolves dynamic model deployment + API base
  -> litellm.atranscription()
  -> native multipart provider config
  -> generic built HTTP request
  -> selected deployment
  -> parsed JSON or document result, or incremental native event stream
```

For translation, this chain is new but structurally equivalent:

```text
HTTP /v1/audio/translations
  -> ordinary pre-call auth + multipart processing + route dispatch
  -> Router.atranslation()
  -> litellm.atranslation()
  -> official OpenAI SDK translator for OpenAI; native config for self-hosted servers
  -> same JSON/document result adapter and gateway return contract
```

Relevant implemented precedents:

- `PYANNOTE_DIARIZATION_PROVIDER_PLAN.md`: complete call-type, route,
  router, SDK, HTTP-handler, configuration, controller, and test plan
- `PYANNOTE_SPEAKER_EMBEDDINGS_PLAN.md`: additive response fields required
  zero gateway schema remap because the provider response stays intact
- `FISHAUDIO_DRAMA3_GATEWAY_GAPS.md`: actual transport kwargs get merged by
  `Router`. Hand-copied incomplete internal field allowlists leak into native
  request bodies. The tests must exercise deployment-injected kwargs
- `additional-features-origin.md`: public facades stay stable and thin;
  isolated behavioral slices carry implementation; drop tests supplement,
  never replace, behavioral tests
- `tests/smoke/AGENTS.md`: scoped `llm_api` customer key, real inputs,
  semantic assertions, no skips. Master-key success alone does not prove a
  customer works

### 2.1 Provider slice boundary

Provider-specific and generic behavior must not be confused:

| Responsibility | Owner |
| --- | --- |
| Public route declarations and standard auth pipeline | existing endpoint-specific transcription route, new thin translation route |
| Dynamic deployment choice, aliases, `api_base`, keys, model name rewriting | existing Router and deployment helpers |
| Distinguishing generic multipart transport from OpenAI SDK transport | transcription SDK plus base native-audio config |
| Native multipart request encoding, duplicate fields, `extra_body` unwrapping | small shared native OpenAI-compatible multipart utility |
| URL path `/audio/transcriptions` versus `/audio/translations` | operation-named provider config |
| Multipart shape options for generic plus NIM variants | reusable native request/response transformer, optionally composed by NIM |
| Exact endpoint protocol errors, error classes | selected provider config |
| NIM event frame parsing into typed generic transcription events | NIM stream slice |
| Terminal state, stream close, total response logging | generic native HTTP stream wrapper plus existing logging lifecycle |
| JSON public cache round-trip | shared transcription cache adapter |
| Data-plane auth, guardrails, and component coverage registration | usual existing maps/groups |

## 3. Decisions and alternatives rejected

### D1. Staged provider activation, no mass registration rewrite

There are genuinely distinct HTTP identities:

- `hosted_vllm/:...` means an OpenAI-compatible server configured by generic
  vLLM-style keys. It currently serves native transcription reasonably well
  (word timestamp generation works) and is the lowest-risk fix registration
- `nvidia_nim/:...` means NVIDIA NIM. Its chat configuration has a much
  narrower OpenAI parameter set than hosted-vLLM. The working audio-chat path
  must not silently change just because transcription is extended
- `nvidia_riva` is gRPC on its own listener, not this NIM and not this plan

**Decision**: implement request/response field, text/document, translation,
and cache behavior against existing `hosted_vllm` first. The resulting
native request/response component is shared rather than duplicated. All
generic provider consumers must keep their existing valid behavior.
Activate `nvidia_nim` HTTP audio only when the server's explicit
deployment declaration proves it is NIM, not merely when `owned_by` or
model id mentions NVIDIA. Multipart streaming remains bounded to that
protocol support, not switched on for generic hosted-vLLM just because an
arbitrary server might emit SSE

Add additive `model_server_name` and `model_server_tag` to the existing
OICM summary/status snapshot, and a composite protocol lookup. The observed
image (`nvidia_nim/nemotron-transcribe-...`) will verify which exact
`model_server_name` value the workspace status API returns, rather than
guessing a new scheme from an image name. The final NIM predicate requires:

```text
explicit model_server_name == observed NIM declaration (or exact allowlisted NIM ids)
AND model_server_tag verifies compatible NIM generation
AND /v1/audio/transcriptions mounted
AND /v1/audio/translations mounted when activating NIM translation too
```

The two endpoint checks ensure a fixed set of semantics instead of loosely
matching any server id. Missing summary facts means fall back to generic
hosted-vLLM behavior, never fail discovery. Unknown future tags decline
NIM-only activation until their contract is checked.

Do not create a new generic `DETECT_FROM_SERVER_NAME` mapping containing
vLLM, SGLang, or every NVIDIA-owned checkpoint. If OICM explicitly declares
`model_server_name=vLLM`, it still does not mean NVIDIA NIM.

### D2. Native OpenAI multipart shape versus raw arbitrary dictionaries

The proxy normalized form is currently `dict[str, object]` and turns
repeating `...[]` fields into list values. Although the generic
`httpx.AsyncClient` encoding accepts some non-scalar `data` values, do not
assume a dict-shaped request defines the provider's multipart semantics.
In particular, repeated parts, lists, bool literal spelling, bracket
notation, and upload tuples must be selected deliberately. Never enclose an
arbitrary dict in multipart `data` and expect an automatic correct protocol

Add a typed intermediate structure whose intent is explicit, built from one
native form-field conversion contract rather than ad hoc provider scripts:

```python
@dataclass(frozen=True, slots=True)
class NativeMultipartFile:
    field_name: str
    filename: str
    content: bytes
    content_type: str

@dataclass(frozen=True, slots=True)
class NativeMultipartRequest:
    # Ordered tuples preserve input order and carry only scalar literals.
    fields: tuple[tuple[str, str | int | float | bool], ...]
    files: tuple[NativeMultipartFile, ...]
```

`httpx` receives `files`: a list of file tuples; `data`: a list or mapping
restricted to scalar values. Boolean form representation needs a repository
probe against NIM (`true`/`false` in lower case with accepted literals)
before freezing the helper. JSON-looking form parts get explicit handling,
not JSON-stringified fields because that changes actual host semantics.

`timestamp_granularities[]` supplied twice reaches the proxy as
`timestamp_granularities: ["word", "segment"]`. The shared encoding must
repeat the underlying native key and preserve the provider-accepted plural
form. Pick the correct emitted plural or square-bracket spelling based on
the NIM's actual schema, not accidental framework syntax. A test server
parses the body with form-conforming parsing and verifies ordered values;
it must not be fed strings and pretend that is enough.

Known OpenAI request parameters (`language`, `prompt`, `response_format`,
`temperature`, timestamp granularities) stay standard. Client native
extensions such as `diarization`, `word_timestamps`, `target_language`, and
future fields stay top-level. Preserved `extra_body` must also unpack into
native top-level form fields after filtering unsafe routing keys (`model`)
rather than encode literal JSON nested text under `extra_body`.

### D3. No module-global AI-owned kwarg filters

`is_litellm_owned_kwarg()` covers canonical internal parameters and the
underscored internal namespace. It cannot prevent future deployment-specific
control params from leaking into native multipart without a general base
contract. It may therefore be used before transport but is no substitute for
actual Router fixture coverage.

Important inputs include inherited router/deployment flags,
`allow_client_keepalive_override`, `keepalive_seconds`, and `use_*` control
flags. Test them explicitly in the equivalent dynamic deployment shape, not
only in unusual direct SDK arguments. `drop_params` must not become a license
to silently alter supported native operations.

### D4. Explicit non-JSON body result, without affecting ordinary OpenAI SDKs

Define a typed envelope for generic multipart JSON documents and plain text
or subtitle responses:

```python
@dataclass(frozen=True, slots=True)
class NativeAudioDocumentResponse:
    body: str
    media_type: str
    charset: str | None
    provider_response_headers: Mapping[str, str]
    _hidden_params: dict[str, object]
```

SDK function signatures widen only for the generic native handler. Return
values under normal OpenAI SDK | Azure | custom-provider channels stay
`TranscriptionResponse`. `NativeAudioDocumentResponse` is not a property on
the `llm_http_handler`; it flows out the generic branch through the proxy to
a `Response`. It can be a wrapper for a provider's `text/plain`, SRT, and
VTT results.

NIM response parsing must be content-aware:

- Native success JSON, including `verbose_json` carrying optional fields:
  parse JSON and convert through the existing `TranscriptionResponse` public
  adapter
- Native `text/plain`, `application/x-subrip`, or `text/vtt`:
  create `NativeAudioDocumentResponse` preserving raw body bytes as decoded
  per declared charset (or UTF-8) and the response content type
- `response_format` influences the expected parser but actual content type
  remains the source for presentation
- Native error status uses the same error handling even when the body is
  plaintext. Never interpret an HTTP error as a successful text transcript
- A NIM text/SRT document never becomes a transcription JSON object merely
  to satisfy the present SDK's `isinstance` check

The ordinary OpenAI SDK `OpenAIWhisperAudioTranscriptionConfig` still converts
text documents into `TranscriptionResponse` for its SDK bound operations.
Do not unintentionally change that public transport. If LiteLLM aliases to
that same generic handler sharing a parser, make an adapter at the boundary
instead of globally loosening every provider's protocol contract.

### D5. Rich JSON via shared cache adapter

`convert_to_model_response_object()` currently rebuilds a small subset of
transcription keys. `text`, optional standardized `language`, `task`,
`duration`, `words`, `segments`, and recognized `usage` survive, but native
private/public additional fields disappear. A cold and cached HTTP response
therefore differs.

Replace this narrowing with a typed `TranscriptionResponse` serializer and
hydrator shared by cache reading and dynamic metadata handling:

- Preserve all dynamic `TranscriptionResponse` public fields previously set
  alongside `text`, not merely five optional keys
- Standardize recognized token or duration usage objects through their
  existing cost-aware conversion helpers
- Keep unrecognized native usage shapes passthrough as public metadata where
  harmless and explicitly marked unstructured for cost calculation, rather
  than rejecting the entire cached response
- Never allow `_hidden_params`, internal provider URLs, and cache header
  artifacts to appear in the public JSON object
- Recreate cache-only internal fields (`cache_hit`, appropriate provider)
  from current request metadata, not a cached prior request
- Validate provider JSON once with a discriminated provider response model
  or `TypeAdapter`. That typed model may preserve additive extension fields
  without repeatedly annotating call sites with `Any`
- A copy for standard logging may replace `usage` with normalized chat
  usage. Do not mutate the public original or a shared cached object while
  doing it, retaining native data for the SDK and public response

The exact serialization mechanism is implementation evidence: inspect
`OpenAIObject.model_dump()` with synthetic dynamic response attributes
before selecting where extras round-trip. If model-dump drops them, central
transcription serialization must use the same public channel that preserves
those attrs on the cold HTTP response; merely copying them into another
Pydantic object's private dictionary does not make them public

JSON versions go through one shared adapter only. Native cache preservation
does not require disabling caching for NIM; keep nonstreaming cache enabled
and verify two full gateway requests

### D6. NIM SSE typed on gateway side; native verified frame vocab

The NIM's native streaming vocabulary includes:

```json
{"type": "transcript.text.delta", "delta": "..."}
{"type": "transcript.text.done", "text": "...", "...native usage and metadata": "..."}
```

The gateway must preserve `transcript.text.delta` and
`transcript.text.done` semantics, and not manufacture chat-completion or
Responses API event names. Data field names might be straightforward JSON
object keys as tested, but implement from the live payload after verifying
its exact schema. Do not claim event ordering or payload content based on
how the OpenAI SDK happens to identify its chunk. `data: [DONE]` terminates
the gateway SSE stream explicitly, with deterministic framing.

Conversation with the worker and future revisions must pin the remaining
verified response usage keys against source discovery, not assume all
transcription metadata goes into done events (different NIM servers and
clients may disagree about `wait`/`include_usage`). The NIM specifics stay
out of the generic base.

Add frozen, discriminated generic event families matching actual
transcript semantics (`TranscriptDelta`, `TranscriptDone`, plus error);
each provider implements a parser/validator producing them. The generic
gateway emitter never knows why the field is significant.

### D7. Translation really is a new first-class operation

Do not call NIM `/audio/translations` through the SDK's `atranscription()`
and change the suffix. That reuses wire multipart machinery but skips the
public call type, the response hook, cost lookup, import/export, and
guarantee that `target_language` stays translation-specific.

Translation keeps public OpenAI-style field names but accepts native extra
parameters exactly as provider config defines them. Its SDK should wrap
`BaseAudioTranslationConfig` analogous to transcription. Defaults cannot
silently turn translation into a simple transcription request

## 4. Finely graded implementation slices

### Phase A: ordinary transcription and response docs

#### A1. Typed native response + nonstream cache adapter

New common types in `litellm/types/llms/audio_transcription.py`:

- JSON result alias integrating existing `TranscriptionResponse`
- `NativeAudioDocumentResponse`
- typed safe native success response metadata
- type alias `NonStreamingNativeAudioResult = TranscriptionResponse | NativeAudioDocumentResponse`

Adapter code should live near the response converter,
`litellm/litellm_core_utils/llm_response_utils/transcriptions.py`, rather
than another large section of `convert_dict_to_response.py`. Keep one
converter for all final JSON keys via JSON-compatible value validation,
then hydrate declared OpenAI fields with existing usage transformation.
Internal attributes and ordinary client-visible body schemas remain distinct.

Modify:

- `litellm/litellm_core_utils/llm_response_utils/convert_dict_to_response.py`:
  delegate audio transcription to shared cache/private transform
- `litellm/caching/caching_handler.py`: only if tests establish the need
  to route streaming separately; existing decorators' streaming gate
  avoids invoking ordinary audio cache for true streaming
- `tests/unit/litellm_core_utils/llm_response_utils/test_transcriptions.py`:
  full cold cache/hydrate round-trip with rich optional values, proper
  hidden param filtering, duration/token standards, and unknown metadata

#### A2. Generic multipart native/audio fields

Modify `litellm/llms/hosted_vllm/transcriptions/transformation.py`:

- Keep `HostedVLLMAudioTranscriptionConfig` as its public class
- Build standard fields through inherited `map_openai_params`
- Source filtering from native params remains the provider config's task;
  preserve optional vendor/extension fields, actual kwargs after filtering,
  and `extra_body` keys. Exclude only canonical internal dispatch kwargs
- Replace current `get_supported_openai_params` iteration in request
  transform, which discards extras before it can send anything
- Accept a scalar and repeated parts native encoder rather than building a
  naive dict containing arbitrary lists, upload contents, or dicts
- Keep `process_audio_file()` for content, filename, content type without
  changing upload contract

Add a small operation-agnostic function under
`litellm/litellm_core_utils/llm_request_utils.py` only for actual shared
filtering and native multipart-form serialization, not for model contract
mapping. It may serve translation code directly if needed. Do not create a
one-use hierarchy of utilities or invent a third provider abstraction.

Choose one transport seam in `litellm/utils.py`:

- `get_optional_params_transcription()` resolves the actual provider config
  before special kwargs are merged
- `add_provider_specific_params_to_optional_params()` gains an optional,
  strongly typed transport discriminator, defaulting to current behavior.
  For generic native multipart transport it appends native fields
top-level. It must not construct nested `extra_body` first and discard it
- The provider transform must also merge a client-supplied top-level
  `extra_body` extension into multipart fields. Preserve `model` from
  deployment resolution, never from unsafe user injection, and verify the
  final emitted native multipart body, not just intermediate mapping
- `_check_valid_arg()` applies only to the public standard transcription
  parameters the provider supports. Vendor extras can remain top-level
  because native backends own their schemas; public standard `stream` is
  included in the capability's accepted map when implemented
- Do not set one universal `uses_openai_sdk=False` for all `hosted_vllm`
  capabilities: native multipart selection must leave chat and other
  embedding/image behaviors alone

Add a capability flag/properties in
`litellm/llms/base_llm/audio_transcription/transformation.py`:

- `uses_native_multipart_transport: bool = False`
- `supports_multipart_streaming: bool = False` (used only by Phase C)
- provider identity, not `model`, chooses via common base. Avoid any
  `if "nemotron" in model` and any model-level lower-case substring

To avoid duplicated transformation implementation in NIM:

- `litellm/llms/nvidia_nim/audio_transcription/transformation.py` contains
  native config with auth fallback `NVIDIA_NIM_API_KEY`, API base
  `NVIDIA_NIM_API_BASE`, URL expression, error class, and response metadata
  handling. Reuse a tested OpenAI-native multipart transformation through
  composition, not inherit hosted-vLLM as though it were the same serving
  software
- Set native multipart and streaming capabilities independently on each
  serving provider; NIM alone opting into SSE must not turn it on for VLLM
- Translation config has a distinct path, not chat-parameter dispatch
- Import classes only through their package exports; provider registry
  supplies corresponding `hosted-vllm` or NVIDIA NIM capability

Phase A extracts reusable hosted-vLLM native multipart request and response
mapping into a composed component used by NIM and hosted-vLLM independently.
Do not make NIM inherit `HostedVLLMAudioTranscriptionConfig`, since that
claims it is the other serving software. Keep existing hosted-vLLM config
class public and its error class, auth, API base, and URL defaults as before

At Phase C activation,
`ProviderConfigManager.get_provider_audio_transcription_config()` adds
`LlmProviders.NVIDIA_NIM` as a thin registry delegate to the NIM-owned
configuration. Configure its independent audio selection even before the
later controller registration migration, and prove it at the unit transport
level. `NVIDIA_NIM` already exists; no new enum is required. Keep the
ordinary generated prefix hosted-vLLM until the compound activation proof

NIM's native stream parser and streaming provider config are usable in
unit/integration tests before discovery migration, but disabled by default
for unrelated multipart services. Live SSE proof completes only after the
selected live deployment has activated the NIM protocol config

#### A3. Natively parsed responses and proxy return

Modify `litellm/llms/custom_httpx/llm_http_handler.py`:

- `_prepare_audio_transcription_request()` still validates headers and
  constructs URL, request data, file values
- Separate provider HTTP request construction from body response waiting
  as a narrow internal helper reused for nonstream and stream
- `transform_audio_transcription_response()` returns the common native
  result union for configs opting into native body delivery
- Preserve standard third-party SDK behavior and policy synthesis for
  `supports_subtitle_synthesis` unchanged. Some providers lack actual
  SRT and use LiteLLM's subtitle synthesis; that remains a different path
- Ensure `set_provider_response_headers_in_hidden_params()` also applies
  to non-JSON provider doc results and native errors use `_handle_error`

Modify `litellm/main.py`:

- `transcription()` and `atranscription()` accept a non-JSON result only
  from native-capable provider branches
- Centralize result handling into a small nonstreaming adapter that calls
  `post_call_success_deployment_hook` and `_llm_caching_handler` on the
  public shape it accepts, not an arbitrary untyped response union
- Before full-content callbacks, derive a `TranscriptionResponse(text=body,
  ...)` view with file duration in its hidden params. Keep this view beside
  the immutable `NativeAudioDocumentResponse`. Send one consistent result
  through standard logging/cost and hook logic rather than a mixture of
  native body and randomly cast `Any`
- If a hook returns a modified response, apply that output document as
  deliberate text if its only public difference is `text`, but flag an
  incompatible hook response as an error. Standard skips/whitelist
  preserving output must not become an undocumented bypass
- Preserve the full native document for public output but let the standard
  cost calculator derive duration from its associated text-view. No
  additional cost lookup based on guessed token pricing for plain document
- `_hidden_params` duration calculation reads the upload once per
  operation and shares it between public result and internal text summary.
  New private native types stay frozen; per-operation logger fields may
  still be populated through their established properties

Modify `litellm/proxy/proxy_server.py`:

- Delegate final route return of the new union to a small generic helper,
  e.g. `litellm/proxy/audio_response_helpers.py`, not a repeated `if`
  block inside two large endpoint implementations
- Standard `TranscriptionResponse` preserves current return and response
  headers
- `NativeAudioDocumentResponse` returns `Response(content=...,
  media_type=...)` and shared model/cache/response-cost headers
- Response header hook at `post_call_response_headers_hook()` participates
  for both shapes. Header building reads explicit hidden-param metadata
- Post-call success/guardrail and response size policy can't be bypassed
  just because body is plaintext; centralized text adaptation if needed
- Once standard headers computed and response body prepared, no response
  type-specific try/except is allowed to turn invalid documents into output
- No upload-streaming feature beyond current `await file.read()`
  limitation required here. Native response streaming does not change
  upload semantics

Behavioral tests:

- Request encoder with true multipart HTTP server, bools, `diarization`,
  `word_timestamps`, two timestamp granularities, an `extra_body`-encoded
  extension, inherited and canonical internal kwargs
- First response and second cached response preserve identical native JSON
  public fields, including meaningful timing fields
- Text/srt/vtt correct status, raw body, media type; assert they are not a
  JSON wrapper with text field
- Backend HTTP 4xx plaintext must surface as native control error, never 200
- Standard OpenAI SDK provider and Azure parsers remain unchanged by prior
  baseline fixtures

### Phase B: full translation operation

#### B1. Native translation base and config

Add:

- `litellm/llms/base_llm/audio_translation/__init__.py`
- `litellm/llms/base_llm/audio_translation/transformation.py`:
  `BaseAudioTranslationConfig` and request contract for native file/form
- `litellm/llms/hosted_vllm/audio_translation/__init__.py`
- `litellm/llms/hosted_vllm/audio_translation/transformation.py`
- `litellm/llms/nvidia_nim/audio_translation/__init__.py`
- `litellm/llms/nvidia_nim/audio_translation/transformation.py`

Both self-hosted configs reuse native audio multipart utilities and result
adapter, not inherit from the transcription module that hardcodes request
path. Hosted-vLLM config exposes the supported OpenAI-compatible
translation parameters. NIM-specific `target_language` reaches NIM
unchanged; it is not aliased to the client extension `language`

Native HTTP handler interfaces:

- factor file/form preparation, request execution and provider error
  wrapping into small shared helpers below `BaseLLMHTTPHandler`
- existing transcription async/sync logic remains same
- `translation_handler()` /
  `async_translation_handler()` calls provider base public methods through
  same dependency-typed config class
- No `base_llm_http_handler.audio_transcriptions(...,
  endpoint="/translations")`-style hacks, because the operation and logs
  must remain recognizable

`ProviderConfigManager.get_provider_audio_translation_config()` delegates
to `oicm_providers.registry` for NVIDIA NIM and/or hosted-vLLM. Config
absent for providers that do not actually expose native translations; do
not try to expose `/v1/audio/translations` for every chat-model endpoint.
The current NIM exposes it, but conditional registration stays bound to
that server's tested transcription+translation native surface.

#### B2. SDK and router behavior

New slice `litellm/endpoints/audio_translation/`:

- `main.py` exports `translation()` and `atranslation()` with standard
  model/file selection, provider config lookup, timing, and client wrapper
- `__init__.py` explicit exports only
- `litellm/__init__.py.__getattr__()` lazy-public import following
  diarization, not eager import of the whole slice
- `@client` finds `atranslation` from decorated public function name and
  flows to call type. Add standalone `translation()` as the synchronous
  literal public function, not the same executor-wrapper function under two
  names
- `atranslation()` places an `atranslation` marker into `get_litellm_params`
  input (including `OPTIONAL_KWARGS_KEYS` where necessary), so
  `is_sync_litellm_request()` sees it in actual proxy calls. A standalone
  branch annotation that never reaches actual runtime is not sufficient
- public SDK signature retains required `model`, `file` and optional
  `prompt`, `response_format`; extension parameters via `**kwargs` flow
  into native provider mapping
- Do not pretend `language` is required for OpenAI translations;
  NIM-specific `target_language` lives within provider config

Router:

- Adjacent to `Router.atranscription()` and `_atranscription()`, add
  `atranslation()` and `_atranslation()`
- fallback wrappers use the exact call-type fields
- `function_setup` handles translation input as audio upload exactly like
  transcription's positional or keyword file input. Compute the checksum
  before API calls for `translation()` as well, even when translation is
  initially excluded from response caching
- do not create an unused OpenAI SDK `model_client` for native-constructed
  multipart; `model_client` should be created only when transport requires
  it. It holds no streaming data and its presence is not code reuse
- correct `--num_retries` behavior, semaphore management, one single
  API-bound deployment slot per operation, method-level counters

Type/registration maps:

- `CallTypes.translation` / `atranslation` in
  `litellm/types/utils.py` and `CallTypesLiteral`
- map `/audio/translations` and `/v1/audio/translations` into
  `API_ROUTE_TO_CALL_TYPES`
- `RouteType` and `ROUTE_ENDPOINT_MAPPING` in
  `litellm/proxy/route_llm_request.py` add `atranslation`
- `_AUDIO_MODEL_REQUIRED_ROUTES` should not need a change, but test the
  native validation (must not reject audio translations for lacking `file`)
- original async-call grouping in `is_sync_litellm_request()` and SDK sync
  determination (`function_setup`) includes `atranslation`
- translation gets `get_provider_audio_translation_config()` and public
  conversion/config types as analogues of STT

Auth/route classification:

- `LiteLLMRoutes.openai_routes` includes both alias paths, just as ordinary
  `/audio/transcriptions`; ensure
  `llm_api_routes` computed snapshot contains them
- include in LiteLLM's OpenAPI auth-related route lists too, not only main
  request blueprints
- `gateway/routes/allowlist.py` covers `/v1/audio/` and `/audio/` already;
  do not duplicate route registration in there unnecessarily
- backend allowlist must not include translation; existing coverage test
  proves new route is on gateway only
- call-types mapping is separate from auth list: one enables guardrail
  behavior, the other allows customer access. Implement both
- `endpoints/audio_translation/main.py` is a slice. The proxy route also
  belongs in a thin `litellm/proxy/audio_translation_routes.py` mounted
  once in `proxy_server.py`. Do not carry route logic into generic SDK

Proxy route:

- ordinary async request signature with `UploadFile = File(...)`,
  `user_api_key_dict`, and `fastapi_response` required as in STT, not a
  wholly simplified `target_language` FastAPI Form declaration that would
  remove unknown fields
- parse `get_form_data`, read uploaded file, validate required filename,
  build standard `metadata` with `add_litellm_data_to_request`, call
  `pre_call_hook` exact `CallTypes.atranslation.value`
- select client-facing model via same `resolve_inference_model()` endpoint
  kind (`body`, deliberately endpoint purpose not moderation)
- `route_request(data=..., route_type="atranslation", ...)`; `result = await llm_call`
- common audio response adapter applies consistent headers to JSON and
  document responses. Translation's native `stream=true` produced a 400
  in the inspected NIM, and that error must remain truthful
- failure hook wraps the entire try; preserve HTTPException status and
  error envelope as ordinary transcription does
- `OPS_ONLY_ROUTES` is empty because translation is inference
- close the in-memory uploaded file in `finally`. Provider network
  transport lifetime is separate and already lasts through callback
  processing; no open stream is needed for Phase B
- adapter strips private transport metadata from public JSON while
  injecting model, call-id, cost, and cache facts into response headers

Deployment behavior:

- `/v1/audio/translations` accepts a client model alias the same way STT
  does; don't hardcode a first eligible vLLM model or search by checkpoint
- same `model_info.mode=audio_transcription` suffices initially because
  translations are a sub-operation of the same deployment
- if a separate `audio_translation` mode becomes needed, separate PR:
  don't alter existing token-budget/capability stamping to fake it now

#### B3. Other supported OpenAI translation providers

The standard OpenAI-translations HTTP operation must be supported too, not
only the currently registered self-hosted NIM. Plain OpenAI can keep its
OpenAI SDK transport, but the new SDK route needs a real translation
provider operation, not a fallback that reads `audio/transcriptions`.

Add `litellm/llms/openai/translation/__init__.py`, `transformation.py`,
and `handler.py` following `openai/transcriptions/handler.py`. The
transformation owns native OpenAI translation optional params and
`/audio/translations` authentication/URL. The small handler calls the
official `openai_aclient.audio.translations.with_raw_response.create(**data)`
and its sync counterpart, following the existing official raw HTTP SDK
request pattern. Shared response conversion then constructs the same
native JSON or document result already used for all audio operations.
That use of the official SDK avoids re-encoding fields it owns

Use this OpenAI provider operation when the deployment selects
`custom_llm_provider == "openai"` and supports audio translation. Keep
`hosted_vllm` and NVIDIA NIM on their generic native multipart handler,
without inheriting `OpenAIWhisperAudioTranscriptionConfig` for an
unrelated path. The route family uses dynamic deployment resolution, so
a static OpenAI model registration can work without a customer-passed
`nvidia_nim` prefix. A real OpenAI key proof belongs in the standard
integration/e2e configuration, not a permanent production smoke suite
assertion if the upstream service cannot translate the chosen fixture

`Extra body` follows whichever transport is actually used. It is an SDK
honored argument in the OpenAI branch; native HTTP receives top-level
multipart fields. Unknown request formats are never silently guessed

#### B4. Translation cost and plugin typing

Existing `_TRANSCRIPTION_CALL_TYPES` in `litellm/cost_calculator.py`
currently contains transcription. Don't quietly add translation and pretend
its provider prices are identical. Instead tokenize:

- `_AUDIO_FILE_TRANSCRIPTION_CALL_TYPES`: transcription + translation
- `_TRANSCRIPTION_CALL_TYPES`: transcription only for cost branches whose
  unit semantics truly apply
- or introduce `_TRANSLATION_CALL_TYPES` and add operation through the
  intended native audio cost provider paths

Both operations can share audio-file duration cost policy (`audio_seconds`,
`cost_per_second`) but model cost lookup key and mode must be decided from
live NIM contract. Inspect final customer `response_cost` on both paths.
For an NIM offering a native rich usage envelope, conversion can be
shared, but translation requires `CallTypes.atranslation.value` to reach
cost selection.

Audit `_call_type_enum`/`LLMResponseTypes`/CustomLogger hooks for every new
message type not just one dataclass. All plugin `call_type` enums should
accept translation, even if per-model processing returns response
unchanged. `function_setup` must derive translation audio hash exactly as
transcription's `file` input.

Add tests wherever the existing cost and hook suite mirrors public call
mapping. The rule here is that translation failing because late dispatch
misses a special case is not acceptable and cost type must never
incorrectly borrow text chat pricing.

### Phase C: multipart NIM incremental SSE

#### C1. Server identity activation gate wiring

This phase requires an explicit source of NIM identity. Implement it by
extending the current task lookup, not by static model config:

1. Add `model_server_name: Optional[str]` and
   `model_server_tag: Optional[str]` to `OicmDeploymentSummary`
2. Add fields with safe defaults to `OicmStatusSnapshot` so construction
   in existing tests does not break. Map in `build_snapshot()`
3. In `controller/controller.py`, `_declared_task()` becomes a small
   `_deployment_declaration(uuid)` returning the snapshot's frozen
   inference declaration, including task and server fields.
   `_declared_task` can remain a small derivation if it has other callers
4. Inject that same duplicate-free lookup into `LocalDeploymentSource`
5. Add `detect_nim_audio_protocol(declaration, paths)` or an equivalent
   pure helper in controller's identity/provider layer:
   observed server id plus compatible tag plus transcribe + translations
   surfaces. It may return `nvidia_nim` only for this compound identity
6. `detect_provider()` continues handling known independently detected
   native services. For compounding identity, do not overwrite known
   `hamsa`/`pyannote`/`omnivoice` providers. Provider detection is per-file
   protocol mapping, not per-checkpoint mutation
7. Add regression fixtures: owned_by=nvidia but vLLM stays `hosted_vllm`;
   model_name contains nemotron but server is vLLM stays `hosted_vllm`;
   declared NVIDIA NIM + both native paths becomes `nvidia_nim`; missing or
   unknown server fact safely stays generic

The live Al Ain pod's observed values must be logged redacted as facts
of registration (server kind, tag, fields changed, not credentials).
Don't derive the server name by splitting its registry URL in a generic
function every sync. Keeping one actual operator-declared field avoids new
heuristics. If verification shows `model_server_name=None`, stop stage C's
activation and ask for an explicit declaration rather than guessing.

#### C2. Native Audio HTTP request object and opened transport

For NIM native audio handler, initial return contract becomes:

```python
OpenedNativeAudioStream[NativeTranscriptEvent]: initialized upstream state,
    public media_type / metadata, async event iterator, aclose()
NativeAudioStream = OpenedNativeAudioStream | NativeAudioResult
```

Maintain the established `HttpxBinaryResponseContent` precedent: it owns a
closeable streaming transport response and is recognized by a first-class
route even before iterating. The new stream follows the same pattern,
except events rather than audio chunks.

Creation behavior:

1. Provider config transforms multipart as normal and accepts
   `Accept: text/event-stream`, `stream=true` propagated as standard field
2. Handler calls `AsyncHTTPHandler.post(..., stream=True)`; it supports
   that exact pattern already (built request -> client.send(req,
   stream=True) -> anchors owner for cleanup)
3. Returned `httpx.Response` still reads headers and status before native
   body parsing; raise error for non-success without rewriting output
4. Validate success Content-Type; cannot parse a final response JSON as
   `text/event-stream` just because form `stream` was requested
5. Construct `NativeTranscriptionStream` only after these checks.
   Provider decoder gets the raw response; wrapper is generic
6. Do not eagerly iterate or `await response.aread()`: query/headers and
   status opened the transport, but consumption awaits proxy iteration

Filter `is_streaming_request` in `@client`. A stream object already exists
here so normal full-response cache-assemble must not consume it. Return it
from async SDK dispatcher, with response metadata propagated but no
`dispatch_success_handlers` before clients see terminal done. For regular
text/JSON results existing client lifetime remains unchanged.

The Python SDK therefore exposes an initialized `TranscriptionResponseStream`
object with `meta`, `hidden_params`, `__aiter__`, `aclose()`, and
`__aenter__`/`__aexit__`. It is not the chat `CustomStreamWrapper`. The
initialized handle lets the proxy inspect response metadata before committing
headers and preserves the transport throughout decoding. Document
`with client.audio.transcriptions.with_streaming_response.create(...)`
compatibility if that is not feasible: exact OpenAI SDK class protocol
may be emulated by a dedicated factory, not silently claimed

Support the synchronous SDK symmetrically. The sync HTTP handler also
builds a request and sends it with a true stream flag, iterating typed
events and owning its synchronous iterator plus close operation. Sharing
event decoding between sync/async paths must not block an active event
loop, and does not conceal a synchronous iterator inside an executor.

#### C3. Generic stream lifecycle plus NIM decoder

Add a small `streaming.py` under `base_llm/audio_transcription/` for
lifecycle and framing. NIM owns its typed event models and
`transform_streaming_audio_response(raw_event)`.

Use the official OpenAI SDK SSE parser already used by
`litellm/responses/streaming_iterator.py`: import `SSEDecoder` from
`openai._streaming` and construct
`SSEDecoder().aiter_bytes(response.aiter_bytes())`. Forward decoded JSON
`data` into the provider decoder. Do not invent a parallel SSE splitter.
`openai` is already the SDK dependency whose source handles partial
boundaries and CRLF. Preserve verification of actual frames across real
network chunk boundaries nonetheless

`SharedNativeTranscriptionStream` responsibilities:

- Own the `SSEDecoder` and byte iterator lifecycle without eagerly
  `await response.aread()`
- Call provider-config `transform_streaming_event(model, parsed_json)` to
  get typed event; unknown keys survive in generic extras to prevent
  metadata loss
- Enforce delta -> terminal `transcript.text.done` event state; require
  exactly one terminal event from NIM, translate `[DONE]` as actual stream
  end marker per verified protocol whichever sequence NIM uses
- Mark success only for terminal done, preserve complete final
  transcription string with optional native extras for logging cost/history
- Handle mid-stream parse error as structured failure; if event contracts
  reject a frame, emit a well-defined error event once (gateway customer
  has headers already), close backend, and finalize failure logging once
- Cancel upstream iteration when caller disconnects or `GeneratorExit`
  arrives, always `await response.aclose()`, and don't create detached tasks
- Finalize failure/success handler exactly once using an atomic terminal
  state transition consumed by one generator/closer. `__aexit__` and
  `aclose()` must be idempotent so repeated disposals don't log twice. New
  state fields go in a dedicated lifecycle dataclass or small wrapper,
  never mutate a public final response
- Honor `LITELLM_MAX_STREAMING_DURATION_SECONDS` with existing patterns;
  route fallback isn't allowed after any delta has reached client. Before
  first output transport error could trigger existing Router fallback only
  if it hasn't committed public headers
- Update completion start times for TTFT, preserve logging IDs/virtual
  key and internal base API. Use existing `Logging` hooks and terminal
  response cost calculator rather than making private pseudo-log callbacks
- Don't log an entire transcript on every delta. End-only normalized
  `TranscriptionResponse` internal summaries reuse existing cost
  computation and audio duration

NIM provider parser responsibilities only:

- `data:` JSON event parsing through `TypeAdapter`, preserving optional
  event-level fields
- map native delta `delta` -> generic delta item with ordering
- map native done `text`, `usage`, timing, other arbitrary extras
- preserve verified `transcript.*` future-compatible extension keys.
  Unknown event types become explicit errors or declared extensions,
  rather than disappearing silently
- native event vocabulary stays in provider decoder; generic serializer
  emits each validated generic event plus its native fields deterministically

The protocol stream wrapper needs an adapter requiring `Logging` dependency
in constructor, with a fake lightweight logger in unit tests; don't
monkeypatch class or static logging methods. `AsyncHTTPHandler` and
response-like body reader must be dependency-injectable transport protocols,
so behavior tests can handshake/delay frames without any network.

#### C4. Proxy route and response hooks

In generic audio response adapter:

- Recognize the `NativeTranscriptionStream`, calculate custom headers
  from explicit `_hidden_params` plus provider responses, then re-use
  `post_call_response_headers_hook`
- On normal deltas, `StreamingResponse(media_type="text/event-stream")`
  forwards deterministic serialized events, doesn't pre-parse vendor keys
- Call request-size/duration/hook/core lifecycle handlers before header
  commit where semantically valid but not main response success hook that
  requires full output
- Streaming's terminal completion uses final normalized log context;
  no premature `update_request_status(success)` before iterator begins
- If `StreamingResponse` construction occurs before iterating, backend
  response must be bound strongly enough it cannot GC/close incorrectly,
  and stream end closes both iterator and `AsyncHTTPHandler` if owned
- Use `background` finalizers only for guaranteed shutdown side-effects;
  never fire terminal log from both closure and generator. Await graceful
  end in generator `finally` using appropriate `aclose` handling for
  cancelled contexts

Allow only negotiated `stream=true` requests into this branch. Handler
tests verify NIM SSE content type and typed events. Dispatch must reject
an unsupported generic multipart stream with clear HTTP 400 while
`supports_multipart_streaming` is false, instead of buffering or creating
untruthful pretend SSE

Proxy guardrails:

- ordinary pre-call input and post-call response guards can consume an
  internal `TranscriptionResponse` summary at completion, not stream events
  individually
- if full-content guardrails require buffering to assess responses, state
  their limitation and test 403 before earliest header where such response
  contract is expected. Do not silently weaken post-call guards solely to
  expedite streaming
- do not wrap them into chat `CustomStreamWrapper`, which has chat-specific
  `.sse` expectations and user token pricing. The transcription stream
  should share only base finalization/primitives it really uses

Audit configured output-redaction/content callbacks against native events
before guaranteeing support. An unavoidable incompatibility is an explicit
pre-headers error, not an undocumented skipped guardrail. Names should be
drawn from actual configured integrations, not invented here

### Phase D: tests, dev-only live delivery, and review

#### D1. *Behavioral* test suite mapping

New tests for new source modules follow `tests/unit/<same path>/test_<filename>.py`
or matching existing provider path convention. Actual existing files whose
behavior changes should be extended rather than adding an isolated copy:

- `tests/test_litellm/llms/hosted_vllm/transcriptions/test_hosted_vllm_transcription_transformation.py` extension for generic native form fields
- New `tests/unit/llms/nvidia_nim/audio_transcription/test_nvidia_nim_audio_transcription_transformation.py`
- New `tests/unit/llms/nvidia_nim/audio_translation/test_nvidia_nim_audio_translation_transformation.py`
- New `tests/unit/endpoints/audio_translation/test_audio_translation_sdk.py`
- Router methods/call semantics in corresponding existing translated test
  suites, not purely structural mocks
- `tests/unit/proxy/oicm_routes/test_audio_translation_routes.py`
- `tests/unit/llms/custom_httpx/test_native_audio_streaming.py` (if module
  split by behavior, mirror precisely)
- `tests/unit/litellm_core_utils/llm_response_utils/test_transcriptions.py`
- Existing related audio cache, response conversion, SDK and client HTTP
  mocking suites for regression

Additional user-facing wiring checks:

- `tests/unit/proxy/test_oicm_drop_detection.py` shallow assertions ensure
  translation route mounted, SDK function lazy-importable, registry
  selects NIM class, RouteType mapping present, public markers wired for
  `atranslation`. Do not implement behavior by testing a source-string `elif`
- Existing `_FORK_INFERENCE_ROUTERS` includes new translation router,
  which means both general test families prove customer route authorization
  and guardrail call-type coverage
- `gateway/routes/tests/test_gateway_plus_backend_covers_full_app` proves
  `/audio/translations` is kept by gateway and not assigned to backend
- `litellm/proxy/litellm_core_utils/litellm_logging` fake logging tests
  validate sync/async call classification with `atranslation` and
  caching setup's file hash includes file content dimensions+model as it
  does for transcription

#### D2. Required outcome mutation scenarios

Every bullet becomes a targeted assertion, checked initially against
unfixed code where practical and against at least one reverted/altered
implementor line after implementation. Aim > 90% usable behavior mutants:

| Mutant | Regression must fail |
| --- | --- |
| `diarization` filtered out or nested in `extra_body` | Local-backend capture expects actual form key/value, not request may still succeed |
| duplicate timestamp becomes one value | Parsing server checks two fields in right order |
| routing `keepalive_seconds` forwarded | Backend rejects internal key; test still ensures extension fields survive |
| `extra_body` forwarded as raw dict to httpx | Content conversion warning/failed provider schema rather than silent success |
| optional JSON keys narrowed by dict allowlist | Cold-versus-cache public JSON block comparison identifies dropped field |
| provider `text/plain` run through `.json()` | test returns 200 body, parser cannot be JSON-only |
| JSON converted to cached Pydantic values with a free-form `Any` | Out-of-schema/log fields typed validation fails or get ignored deliberately |
| route type misdispatched to transcription | Deployment record observes `/v1/audio/translations`, model kwargs balanced |
| target_language lost | Translation backend rejects missing required target, asserts native independent field |
| translated result priced through plain-chat branch | Cost-profile asserts audio duration pricing path (no exact vendor price) |
| `stream` stripped by a collection allowlist | Gateway body of copy must show `text/event-stream`, never JSON buffered copy |
| stream uses eager read | Delaying first response test first event observed before server completes |
| Naive partition split cuts multibyte token or two events | Deterministic varying network partitions give equivalent decoded events |
| Terminal done raised before last delta | Test stops and asserts builder finishes with proper failure |
| terminal success dispatched twice | logger verifies exactly one normalized completion and two enabled callbacks |
| Cancel/disconnect ignores response close | Fake response cancellation/close await, no task running |
| Success handler invoked before iterator done | Event timestamps/ordered fake log calls ensure no early success |
| Route mount exists but missing allowed_routes | Ordinary `key_type=llm_api` rejects/accepts exactly correct code, not masked by master key |
| Cost/metadata `_hidden_params` dropped | Model, model-id, response-cost headers and log cost consistent with JSON retrieval |
| NIM activated from `owned_by` or `Nemotron` id | vLLM fixtures with those strings stay `hosted_vllm` |

For final test quality/mutation gate do not introduce a hundred meaningless
logical comparison replacements just to raise coverage. Test user-visible
errors and protocols caused by real problems first.

#### D3. Live dev-only proof

Resolve the NIM Service and pod addresses through discovery at proof time.
Do not hardcode the deployment UUID as the gateway's long-lived
registration. Public model ID remains unchanged. Start with read-only dev
deployment inspection:

```bash
export KUBECONFIG=$HOME/.kube/alain-oicm.conf
kubectl -n adeo-litellm get deploy litellm-proxy-dev  # read only baseline
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000
```

Dev has **no ingress**. The HTTPS gateway hostname reaches prod and the
broker gRPC tunnel, not dev REST. Rebuild/restart dev pods after image
changes, then restart the forward. No prod model registration or workload
edit is part of this dev verification

Use a small real speech fixture for semantic JSON and plain-text checks.
Use a separately validated speech fixture for `language=en`, verbose
JSON, and `diarization=true`. Short test utterances do not prove detailed
timestamps. English audio through translation isn't multilingual coverage
accuracy evidence; record fixture limitations in the updated guide

Basic curl proof pattern:

```bash
curl -sS -D /tmp/nemotron_headers.txt \
  "http://localhost:14001/v1/audio/transcriptions" \
  -H "Authorization: Bearer ${KEY}" \
  -F "model=${NEMOTRON_MODEL}" \
  -F response_format=text \
  -F "file=@${NEMOTRON_AUDIO}"
```

Assertions inspect returned status/header and actual document body, not just
nonzero HTTP. For streaming `curl --no-buffer --max-time ...`, print
arrival timestamps with a bounded stdlib parser/probe, keep the actual curl
command, and assert a delta precedes done and stream termination.
Probe notes don't replace direct live curl proof; they only help measure
incremental arrivals.

Baseline vs new live expected checks:

1. **JSON vs native**: direct NIM and gateway receive the same real file
   and native fields; compare public semantic fields, not exact inference
   text. Capture required baseline metadata only, no huge payload logs
2. **First and cached JSON**: submit identical nonstreaming request twice
   with measurement disabled client-side caching; get `x-litellm-cache-key`
   or equivalent cache hit marker proves second retrieval, public rich
   fields remain every value supported by NIM baseline
3. **Text, srt, vtt**: correct media type and nonempty document.
   Do not pin vendor timing values, formatting strings, or a transcript
   literal. Verify no JSON envelope leaked into known plaintext responses
4. **Translation**: `POST /v1/audio/translations`, model + file +
   `target_language=en` returns 200 and native text/usage. Server-only
   unknown/unsupported `stream=true` still returns 400, proving endpoint
   was right and options not rewritten
5. **Incremental SSE**: gateway content type, first delta observed during
   serving while NIM still streaming not after entire done; final normalized
   reconstruct transcript nonempty, native keys present in final event,
   all as verified NIM's payload form, no large trace of backend body
6. **Authorization**: actual newly generated `key_type=llm_api` key with
   restricted models (NIM only) must 200 custom ops; `key_type=read_only`
   denies 403; no key yields 401. Delete temporary keys afterward using
   standard resource cleanup. Master key must not be sole customer proof
7. **Header & logging**: model ID, request ID, response cost, key
   appropriate duration spent row consistent between JSON, text and
   stream. Use DB/metrics/admin call read only and confirm transient log
   entries completed rather than pending. Test both proxy workers in dev
   environment if more than one occurs; HTTP request retries not used to
   hide behavior problems
8. **Chat streaming**: `POST /v1/chat/completions` on same successfully
   serving model naming, an inline wav and text transcription instruction,
   stream true includes chat `choices[].delta.content`, possible usage,
   `[DONE]`; native metadata retained. NIM chat operations not modified
9. **Guardrails and routing**: no fallback to
   `allm_passthrough_route` or `pass_through_endpoints`. Relevant guardrail
   call-type mappings use translation and not just a transcribe alias
10. **No prod rollout**: capture dev-validated image tag/digest and pod ID.
    Never promote prod or add unmanaged NIM rows just to work around a
    missing server identity declaration

Record dev image config digest and pod ID, controller image if synced,
port-forward setup, request/response headers, and public body shape. Never
include API key values, credential-encrypted payloads, client identities,
or customer names in implementation docs or PR proof

#### D4. Smoke/functional suite and hooks

`oicm-litellm-layer/tests/smoke/test_model_contract.py`

- Extend `TestSpeechToText` with generic native NIM cases after fixtures
  access is resolved, not a bespoke one-off new folder of tests
- Use semantic `text` presence and vendor metadata equality between direct
  native baseline and gateway; don't pin exact generated transcript or
  timings outside my own code guarantee
- Tiny valid audio can be unsuitable for timestamps (short NIM smart clip
  yields single word). Use an appropriate existing sample, never silently
  swap to synthetic silent WAV and call that semantic proof
- Streaming test reads raw chunks through httpx with standard SSE
  parser, proves first `transcript.text.delta` arrives before terminal
  done, and does not error on trailing separators
- Translation model_contract belongs same feature class as STT. Customer
  auth matrix can extend `test_contract.py` to prove route not 404 and
  right 401/403 envelope (the actual 200 inference must scoped customer)

If gating stage C because server declaration is missing, do not leave an
active smoke test permanently skipped. Only merge enabled phases' tests
into normal smoke. Do not fabricate server capability declaration to
fulfill a skip policy. When server declaration and activation arrive,
complete feature and add required suite in same resumption PR.

#### D5. Workflow and repository quality gates

- Always inspect default branch with
  `python3 scripts/default_branch.py --branch`
- Pull before starting worker, preserve untracked existing
  `evaluate-delete-later/` and other local untracked fixtures unrelated
  to requested plan; don't add them
- For new source modules, mirror into `tests/unit/`. For existing mapped
  providers, extend the existing test file rather than moving it
- No `Any` skip shortcuts, mutable Shared Pydantic Response, global
  rebinding of flags, mutable native form dict transformations. Use
  tuples/immutable intermediates `MappingProxyType()` and `Final`
- Every resolved typed dict field must `ReadOnly`, pydantic model
  frozen, no blanket `# type: ignore`. Reasoned unavoidable suppression
  exact rule code + reason only
- Immutable handling of legacy `TranscriptionResponse`: adapter constructs
  a new instance or `model_copy(update=...)`, never mutates shared data
  response caches after set. Existing public mutable response class is
  not changed into frozen model (compatibility), only new private types
  respect this rule
- Validate raw HTTP JSON at its boundary before passing typed values
  through provider functions, avoiding `dict[str, Any]` through the stack
- Logs and provider errors are normal expected control contract. No broad
  swallowed exceptions turning an erroneous upstream result to empty text
- Narrow static type checking of all touched files, including cascade:
  common handler enum union branch return changes, final routes and cost

After full functional behavior and actual dev proof:

```bash
make check
```

Its output is saved under `.git` and path printed at both ends; read that
log rather than repeating multi-minute gate to see a different segment. A
machine-wide slot contention message is normal queue behavior, not a hung
test. Do not set `LITELLM_GATE_SLOTS=0`.

When lint or type gates reject the request flow, prefer immutable
validated mappings to any suppression. Distinguish branch-introduced
violations from preexisting ones with their logs. Never raise `ANY_CAPS`
or lower a coverage/lint threshold

Report failing behavioral mutants as blockers, not as incidental test
noise. Do not use a relay as alternative live proof

## 5. Concrete file inventory

### New source files

```text
litellm/types/llms/audio_transcription.py
litellm/litellm_core_utils/llm_response_utils/transcriptions.py
litellm/llms/base_llm/audio_transcription/streaming.py
litellm/llms/base_llm/audio_translation/{__init__,transformation}.py
litellm/llms/hosted_vllm/audio_translation/{__init__,transformation}.py
litellm/llms/nvidia_nim/audio_transcription/{__init__,transformation,streaming}.py
litellm/llms/nvidia_nim/audio_translation/{__init__,transformation}.py
litellm/llms/openai/translation/{__init__,transformation,handler}.py
litellm/endpoints/audio_translation/{__init__,main}.py
litellm/proxy/audio_translation_routes.py
litellm/proxy/audio_response_helpers.py
```

These need not proliferate into individual private modules for each helper.
Provider classes stay thin, NIM specific event schema lives nearest NIM,
and base/native transport helpers under base handler.

### Focused existing source file edits

```text
litellm/__init__.py
litellm/main.py
litellm/utils.py
litellm/router.py
litellm/cost_calculator.py
litellm/types/utils.py
litellm/llms/base_llm/audio_transcription/transformation.py
litellm/llms/custom_httpx/llm_http_handler.py
litellm/llms/hosted_vllm/transcriptions/transformation.py
litellm/llms/oicm_providers/registry.py
litellm/litellm_core_utils/llm_request_utils.py
litellm/litellm_core_utils/llm_response_utils/convert_dict_to_response.py
litellm/proxy/proxy_server.py
litellm/proxy/route_llm_request.py
litellm/proxy/_types.py
litellm/proxy/litellm_pre_call_utils.py (only if metadata/call_type needs)
litellm/litellm_core_utils/litellm_logging.py
litellm/litellm_core_utils/get_litellm_params.py
litellm/proxy/common_request_processing.py
```

### Controller declaration footprint (Phase C)

```text
oicm-litellm-layer/controller/status/{wire,snapshot,builder}.py
oicm-litellm-layer/controller/controller.py
oicm-litellm-layer/controller/{models,sources/local_deployments}.py
```

`reconciler.py` only if provider prefix change generated ordinary sync
already sufficient (likely no change). `build_oicm_model` supports provider
default as unchanged hosted_vllm because stopped deployments cannot route.
Explicit live full sync overwrites NIM prefix only when activation contract
found. No arbitrary static controller "MODEL_SERVER=nvidia" fallback.

## 6. Full chain checklists for translation (anti-dead-end table)

| Capability | Required concrete checkpoint |
| --- | --- |
| Path available | `/v1/audio/translations` and `/audio/translations` mounts exist on `gateway` app, not UI backend |
| Customer auth | both ordinary `/v1/...` and alias included in `openai_routes` and resolved `llm_api_routes`, restricted model allowed |
| Call type | `CallTypes.translation`/`atranslation` flows into `function_setup`, logging call enum and callback hook |
| Call-type route mapping | `/audio/translations` maps to those call types in `API_ROUTE_TO_CALL_TYPES`; mapping exists for guardrails independent of auth |
| Dispatch | `atranslation` present both `RouteType` and route-to-operation mapping, trigger standard `route_request` without generic bypass |
| Deployment | `Router.atranslation()` resolves dynamic deployment `api_base`; no hardcoded NIM address |
| SDK path | `litellm.atranslation` / lazy import present; `translation()` same function through `client` captures operation not transcription |
| Provider registry | `ProviderConfigManager.get_provider_audio_translation_config` returns bounded generic or NIM config as appropriate |
| HTTP generic chain | generic `translation_handler` calls config-provided URL `/v1/audio/translations`, multipart request and error mapper |
| Multipart | bytes content, name and media type preserved from proxy; `model`, `target_language`, and extension fields encoded correctly |
| JSON | native fields on response survive provider parsing plus hidden headers |
| Cache | file content hash used as cache identity, doesn't collide with ordinary transcription or a different model/operation |
| Cost/duration | knows `translation` is audio op, uses actual duration data, same log pipeline but no unsupported invention of price |
| Post-call | input/output audit hook doesn't bypass without guardrail; guardrails get standard result union and type union covers text docs too |
| Component filters | gateway-vs-backend coverage harness recognizes new route once only, no backend mirror declared |
| Upstream merge isolation | actual endpoint behavior in slice and import/export anchor; shallow wiring drop tests check exactly required points |

No implementation stops with SDK import table or FastAPI route if this full
chain is unfulfilled. Learned precedent: `/v1/diarize` once existed while
customer-scoped keys were 403 due missing group; master-key curl cannot be
used to claim completion.

## 7. Rollout order, rollback and risk

1. Deliver generic multipart fields, rich JSON, and native documents
   without changing the hosted-vLLM discovery prefix. Existing chat
   OpenAI-compatible behavior remains unchanged. Reposition associated
   tests only where the source behavior genuinely moved
2. Deliver translation with the generic multipart operation and imported
   OpenAI SDK translator. Prove auth, cost, callbacks, both native paths,
   and public application route behavior end to end before promotion
3. Introduce additive NIM server identity and compound activation. Run
   one dev reconcile, verify its stored row prefix, then restart dev
   gateway and repeat native audio and chat-audio evidence. No prod
   changes yet
4. Deliver native NIM SSE parsing and the generic output lifecycle, then
   incremental dev proof, smoke, full affected suite, and `make check`
5. Incomplete fields from any later phase must not leave a mounted public
   path that routes to the wrong API. Ship phases as independent commits
   or independent PRs; optionally group small cohesive changes by desire

Stages A and B keep the current `hosted_vllm` prefix. Rollback is dev
only, using the prior gateway image. Once NIM activation occurs,
rollback coordinates compatible controller and gateway deployments.
Always disabling NIM activation is simpler than forcing an older gateway
image to understand the new native protocol, and never do rollback tests
on production.

| Risk | Mitigation |
| --- | --- |
| NIM narrow chat param mapper drops existing functionality | audio selection config separate chat; live input audio chat smoke unchanged after registration |
| On-cache transformed data differs | shared internal/public lossless JSON round-trip unit + real repeated requests |
| Proxy interpretive native fields reintroduce DRY problem | base `TranscriptionResponse` support, no filenames containing 'nemotron', no vendor constant lists in route |
| Multi-valued, boolean native forms malformed | HTTP multipart parsing test server and backend actual request contrast |
| Rate limit/semaphore leak or premature logging | test exception cancellable stream, metrics negative balance + close count; no detached shadow finishers |
| Request fallback causes duplicate transcript | no after-first-event Router retry; pre-headers errors semantics pinned |
| Cost types convert raw token usage incorrectly | retain native usage parse if recognized; generic duration fallback uses extracted native response or uploaded audio exactly once |
| Provider inference misclassifies other model servers | compound explicit declaration and service methods, negative fixtures |
| Global native response shape drift | generic protocol union touched tests incl synchronous functions, separate OpenAI SDK and Azure baseline parsers, downstream callback contracts |
| Read translation mode of unrelated chat pod | bound config registration to actual transcribe+translate working server / config present not public provider enum alone |

## 8. Definition of done

Require every applicable goal for the enabled phase. A phase that has not
mounted a public feature does not pretend to have delivered it. Mark its
remaining live proof explicitly deferred, and do not mount a dummy route

- Client and backend extension fields reach the native multipart parser,
  with no per-feature key list required in the gateway
- Rich JSON cold/cache responses have equivalent public fields
- Plain text, SRT, VTT have their native document bytes. They are not
  client JSON `text` fields delivered under inappropriate `Content-Type`
- Translation has the whole chain, dynamic model, and audio duration cost
- Multipart SSE is typed transcript delta/done and actual SSE framing,
  never merely a body forwarded as arbitrary bytes
- Existing affected audio-provider suites as well as NIM cases pass
- Scoped customer-key dev curl proofs finish their redis/DB/active-task
  cleanup or confirms safe expected background tasks
- Request/response headers, logs, and cost are accounted for on every
  response medium positively accepted by the endpoint
- Shallow wiring tests supplement feature tests and scoped live proof
- `make check` runs after dev proof. Mutation tests catch a removed
  feature or behavioral regression, not solely file structure
- Update public guide examples and compatibility restrictions, with no
  claim microphone-realtime operation
- No forbidden pass-through or relay route or transport is used

### 8.1 Documentation owed with implementation

- Revise `docs/usage-guides/NEMOTRON_TRANSCRIBE_GUIDE.md` after dev proof:
  native field preservation, native streams, required target_language,
  response_format vs actual media type, cache semantics any operator notes
- Add plan link to `docs/custom-providers/README.md` and
  `mkdocs.yml` custom-providers section; proposed plan is tracked here already
- Update the existing docs-map link for this plan and the usage-guide
  purpose; keep other protocols' historical plans separate
- Record deviations and delivered state per phase, including the exact
  observed model server or tag, never credentials

---

## 9. Delivered state record

### Phase A (ordinary transcription and response docs), delivered 2026-11-19

Commit `70ba8913b93` on `jya0-v1.108.5`. Native multipart fields, rich JSON
cold/cache parity, and `NativeAudioDocumentResponse` for text/srt/vtt landed
without a live dev proof of the document formats; the Phase B proof below
found and fixed four latent document bugs (frozen-dataclass metadata
stamping, router fallback-header decoration, cache serialization, response
header loss) that unit tests had not covered, and also extended spend logging
to recognize document responses. `response_format=text/srt/vtt` is verified
live in the Phase B record

### Phase B (translation), delivered 2026-11-20, dev gateway `litellm-src:jya0-v1.108.5`

Observed model server: deployment `j-3fb782ae-36cc-45a4-9f65-2a8fdb2029fa`
in namespace `adeo`, served model id
`nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B`, registered as `hosted_vllm`
with the pod at
`s-3fb782ae-36cc-45a4-9f65-2a8fdb2029fa.adeo.svc.cluster.local:8080/v1`.
All proofs ran against the dev gateway over
`kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14001:4000`,
never through the public ingress

Delivered: `/v1/audio/translations` and `/audio/translations` mounted and
authorized for `key_type=llm_api` keys, `atranslation`/`translation` SDK
entrypoints, Router and `route_request` dispatch, `hosted_vllm` and
`nvidia_nim` translation configs (NIM activated only through its own
declarations), openai-transport translation via the OpenAI SDK, duration-based
cost plumbing, namespaced response-cache identity
(`translation:<checksum>`, transcription entries unaffected), spend logging
with `call_type=atranslation`, and true document responses for
`response_format=text`

Live dev proofs, all with the reference WAV `tests/gettysburg.wav`:

- translation cold 200 with `target_language=en`, text genuinely translated
  ("Four score and seven years ago" answered as "Eighty-seven years ago");
  cold and cached responses have identical public JSON `{"text", "usage"}`
- omitting `target_language` returns 200; the pod applies its own default and
  the gateway adds no field
- translation `response_format=text` returns `text/plain` document bytes;
  cold and cached bodies are byte-identical
- transcription `response_format=text/srt/vtt` return `text/plain`,
  `application/x-subrip`, `text/vtt` true document bytes, no JSON envelope;
  cached copies byte-identical
- translation `stream=true` surfaces as a truthful 400: this pod answers the
  nonstreaming call with an SSE stream, so the gateway reports that streaming
  is unsupported for the operation instead of failing to parse `data:` frames
  as JSON. Deviation from the earlier native test recorded in the usage guide:
  the pod streams rather than rejecting
- a scoped `key_type=llm_api` key (alias `phaseb-proof-20261120`, models
  scoped to the Nemotron id) called both alias paths with 200, unauthenticated
  calls 401, and the key was deleted via `/key/delete` with re-use then 401
- spend rows are written for `atranscription`, `atranslation`, and document
  responses with the correct call type (spend is 0.0: this self-hosted
  deployment carries no price entry)
- custom `x-litellm-*` headers (call-id, model-id, cache-key, durations) ride
  every audio response, cold and cached
- chat-audio regression: `/v1/chat/completions` with base64 `input_audio`
  and `stream=true` still returns 200 SSE with a terminal `[DONE]`

---

## Appendix A: Implementation-time facts to verify, not code assumptions

These are intentionally separated from ready coding constraints:

1. OICM `model_server_name` actual value for `j-3fb782ae...`, not from
   intuition. May not be populated, which blocks NIM registration identity
   change only, not ordinary multipart fixes
2. NIM's exact event `data:` mappings for native transcript during and final
   `transcript.text.done`, including proper string field names and optional
   terminal usage shape
3. Native translations output formats. Transcription streaming support
   must not imply translation streaming
4. Preserve NIM's known `prompt` backend validation error as an upstream
   400; never turn it into a different successful operation
5. Map token/duration usage by typed schema. Don't treat audio seconds as
   text tokens
6. Verify how existing `TranscriptionResponse.model_dump()` exposes
   dynamic public attrs versus private `_hidden_params` on both cold and
   cache-hit responses
7. Require compatible controller server name/tag, not a broad prefix that
   would implicitly accept future matching packages
8. Confirm response-header and success-hook paths accept document/stream
   results, with appropriate adapters at normal proxy route boundary

## Appendix B: Ready worker brief

Implement this plan in phases A, B, and C with separately reviewable
commits. Read the usage guide and ordinary transcription stack plus the
implemented pyannote slice, then establish the existing test baseline.
Deliver behavioral test proof and small live dev curl proofs before
`make check`. Resolve every assumption where native evidence is
required. Missing NIM declaration blocks only the provider activation
portion, not generic field and cache fixes. Do not edit live registrations
manually, roll prod, introduce a new checkpoint-named provider, or use
any pass-through command, raw byte relay, or relay-specific route
