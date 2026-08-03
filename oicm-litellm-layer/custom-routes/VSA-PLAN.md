# OmniVoice Missing Routes: VSA Plan

> **Scope**: Implementation plan for `/v1/audio/script`, `/v1/voices` (GET), `/v1/voices/profiles` (CRUD) following the logic mapping technique Phase 3 (Build).
>
> **Reference**: [CLONE-LOGIC-MAP.md](CLONE-LOGIC-MAP.md) — the 7-layer trace of `/v1/audio/speech/clone`

## Design Decision: First-Class Routes vs Pass-Through

The OmniVoice pod's api_base is dynamically discovered by the OICM controller and stored in the router's deployment `litellm_params`. LiteLLM's built-in `pass_through_endpoints` config forwards to a static `target` URL, which cannot resolve the dynamic pod address. Therefore all three missing routes need the full 7-layer treatment: proxy route → router (deployment lookup) → SDK function → HTTP handler → provider config → pod.

However, unlike voice clone (which needed a new config class because of multipart form data), these routes can reuse the existing handler infrastructure more directly. The key insight from the clone trace is that `async_text_to_speech_handler` already dispatches on body type (`dict_body` vs `form_data`), and the `voice_handler` already handles JSON voice operations. The missing routes need their own handler entry points because they don't fit the TTS or voice-creation shapes.

## Architecture: Where the Code Lives

The oicm-litellm-layer integrates with the proxy via:
1. PYTHONPATH set to the layer directory (local dev) or hooks mounted at `/app/litellm_hooks` (Docker)
2. `LITELLM_WORKER_STARTUP_HOOKS` env var for startup-time code execution
3. Config callbacks referencing `litellm_hooks.*` modules

Custom routes cannot be added via hooks (CustomLogger has no route registration). They must be added to the FastAPI `app` at startup. The approach:

1. **Provider configs** live in `litellm/llms/omnivoice/` (inside the litellm source tree, same as existing TTS and clone configs). These are the Layer 6 classes that build requests and parse responses.
2. **SDK functions** are added to `litellm/main.py` (Layer 4) and `litellm/router.py` (Layer 3), following the existing `aspeech` / `acreate_voice` pattern.
3. **Proxy routes** are added to `litellm/proxy/proxy_server.py` (Layer 1), following the existing `audio_speech_clone` pattern.
4. **Route dispatch mappings** are added to `litellm/proxy/route_llm_request.py` (Layer 2).
5. The `custom-routes/` folder in oicm-litellm-layer holds the logic map, VSA plan, and testing methodology documentation. The implementation code goes into the litellm source tree because that's where the proxy server and provider configs live.

## Slice Breakdown

### Slice 1: `/v1/voices` (GET) — List Voices

**Priority**: Simplest. Simple GET, returns JSON, no request body.

**Pod behavior**: `GET /v1/voices` returns a JSON array of voice presets (5921 bytes, 15+ voices).

**Layers to add**:

| Layer | File | Change |
|---|---|---|
| 1 | `proxy_server.py` | Add `@router.get("/v1/voices")` route |
| 2 | `route_llm_request.py` | Add `"alist_voices"` to route_type and ROUTE_ENDPOINT_MAPPING |
| 3 | `router.py` | Add `alist_voices()` / `_alist_voices()` methods |
| 4 | `main.py` | Add `alist_voices()` function |
| 5 | `llm_http_handler.py` | Reuse existing `async_voice_handler` or add `async_list_voices_handler` |
| 6 | `litellm/llms/omnivoice/voice/transformation.py` | Add `OmniVoiceVoiceConfig` with `get_complete_url → /v1/voices` and GET support |

**Borrowed functions**: `OmniVoiceModelInfo._resolve_base()`, `get_async_httpx_client()`, `user_api_key_auth`, `add_litellm_data_to_request()`, `ProxyBaseLLMRequestProcessing.get_custom_headers()`

### Slice 2: `/v1/audio/script` (POST) — Multi-Speaker Script Synthesis

**Priority**: Medium. POST with JSON body, returns binary audio.

**Pod behavior**: `POST /v1/audio/script` accepts `{script: [{speaker, text}], speakers: [{speaker, voice}]}`, returns WAV audio (63884 bytes).

**Layers to add**:

| Layer | File | Change |
|---|---|---|
| 1 | `proxy_server.py` | Add `@router.post("/v1/audio/script")` route |
| 2 | `route_llm_request.py` | Add `"ascript"` to route_type and ROUTE_ENDPOINT_MAPPING |
| 3 | `router.py` | Add `ascript()` / `_ascript()` methods |
| 4 | `main.py` | Add `ascript()` / `ascript()` functions |
| 5 | `llm_http_handler.py` | Reuse `async_text_to_speech_handler` (already supports `dict_body`) |
| 6 | `litellm/llms/omnivoice/script/transformation.py` | New file: `OmniVoiceScriptConfig` |

**Key difference from clone**: Uses `dict_body` (JSON POST) not `form_data`. The handler already supports this path. The config's `transform_text_to_speech_request` returns `TextToSpeechRequestData(dict_body={...})`.

**Borrowed functions**: `async_text_to_speech_handler()` (reused as-is), `OmniVoiceModelInfo._resolve_base()`, `_collect_passthrough()`, `HttpxBinaryResponseContent`, `_audio_speech_chunk_generator()`

### Slice 3: `/v1/voices/profiles` (CRUD) — Voice Profile Management

**Priority**: Most complex. GET/POST/PUT/DELETE, POST needs multipart.

**Pod behavior**:
- `GET /v1/voices/profiles` → 405 (method not allowed on pod)
- `POST /v1/voices/profiles` → 422 (needs `profile_id` + `ref_audio` multipart)
- `PUT /v1/voices/profiles/{id}` → updates profile
- `DELETE /v1/voices/profiles/{id}` → deletes profile

**Layers to add**:

| Layer | File | Change |
|---|---|---|
| 1 | `proxy_server.py` | Add 4 routes: GET, POST, PUT, DELETE for `/v1/voices/profiles` |
| 2 | `route_llm_request.py` | Add `"aprofile_voice"` route types |
| 3 | `router.py` | Add profile CRUD methods |
| 4 | `main.py` | Add profile CRUD SDK functions |
| 5 | `llm_http_handler.py` | Add profile handler (multipart for POST, JSON for PUT/DELETE) |
| 6 | `litellm/llms/omnivoice/profile/transformation.py` | New file: `OmniVoiceProfileConfig` |

**Borrowed functions**: `OmniVoiceVoiceCloneConfig.transform_text_to_speech_request()` pattern for multipart, `OmniVoiceModelInfo._resolve_base()`, `_collect_passthrough()`

## Borrowed Functions Inventory

Functions to reuse from existing codebase (per logic mapping technique "borrow as many functions as possible"):

| Function | File | Used by |
|---|---|---|
| `OmniVoiceModelInfo._resolve_base()` | `omnivoice/common_utils.py` | All slices |
| `_collect_passthrough()` | `omnivoice/common_utils.py` | Slice 2, 3 |
| `OMNIVOICE_INTERNAL_PARAMS` | `omnivoice/common_utils.py` | Slice 2, 3 |
| `get_async_httpx_client()` | `custom_httpx/http_handler.py` | All slices |
| `async_text_to_speech_handler()` | `custom_httpx/llm_http_handler.py` | Slice 2 (reused as-is) |
| `user_api_key_auth` | `proxy/auth/user_api_key_auth.py` | All slices |
| `add_litellm_data_to_request()` | `proxy/litellm_pre_call_utils.py` | All slices |
| `ProxyBaseLLMRequestProcessing.get_custom_headers()` | `proxy/common_request_processing.py` | All slices |
| `_audio_speech_chunk_generator()` | `proxy/proxy_server.py` | Slice 2 |
| `HttpxBinaryResponseContent` | `types/llms/openai.py` | Slice 2 |
| `TextToSpeechRequestData` | `base_llm/text_to_speech/transformation.py` | Slice 2 |
| `process_audio_file()` | `litellm_core_utils/audio_utils/utils.py` | Slice 3 (POST multipart) |
| `get_form_data()` | `proxy/proxy_server.py` | Slice 3 (POST multipart) |
| `route_request()` | `proxy/route_llm_request.py` | All slices |
| `async_get_available_deployment()` | `router.py` | All slices (via router method) |
| `_update_kwargs_with_deployment()` | `router.py` | All slices (via router method) |

## Implementation Order

1. **Slice 1** (`/v1/voices` GET) — simplest, validates the pattern
2. **Slice 2** (`/v1/audio/script` POST) — reuses TTS handler, validates JSON body path
3. **Slice 3** (`/v1/voices/profiles` CRUD) — most complex, builds on slices 1+2

Each slice is tested against the live pod before moving to the next (Phase 4: Verify).
