# OmniVoice Provider: Logic Mapping

> **Scope**: Full endpoint-by-endpoint analysis of the OmniVoice API mapped to the LiteLLM 7-layer architecture, identifying which endpoints need first-class provider integration and which can be handled by existing infrastructure

## OmniVoice API Surface

OmniVoice (k2-fsa/omnivoice-server) is an OpenAI TTS-compatible server with custom extensions. The pod serves on port 8080 and is discovered via the OICM controller's local deployments source.

### Endpoints discovered from /openapi.json

| Endpoint | Method | OpenAI-standard? | Custom params |
|---|---|---|---|
| `/v1/audio/speech` | POST | Yes | `language`, `num_step`, `guidance_scale`, `denoise`, `t_shift`, `position_temperature`, `class_temperature`, `duration`, `layer_penalty_factor`, `preprocess_prompt`, `postprocess_output`, `audio_chunk_duration`, `audio_chunk_threshold`, `request_timeout_s`, `speaker` |
| `/v1/audio/speech/clone` | POST | No (non-standard) | `ref_audio` (file upload), `text`, `voice`, `speaker`, plus all standard TTS params |
| `/v1/audio/script` | POST | No (non-standard) | `text` (multi-speaker script), `speakers` (list of voice profiles), plus TTS params |
| `/v1/voices` | GET | No (non-standard) | None (returns list of available voices) |
| `/v1/voices/profiles` | GET/POST/PUT/DELETE | No (non-standard) | CRUD for voice cloning profiles |
| `/v1/models` | GET | Yes | None |

### Key differences from standard OpenAI TTS

1. `instructions` param causes 422 (not supported by the server)
2. `voice` supports: OpenAI presets (`alloy`, `echo`, etc.), `auto`, `design:<attributes>`, `clone:<profile_id>`
3. `speaker` param for multi-speaker or preset voice selection
4. Voice cloning is one-shot via `/v1/audio/speech/clone` (multipart upload of reference audio), not a two-step protocol like hamsa
5. No `/v1/chat/completions` endpoint (TTS-only server)
6. No `/v1/audio/transcriptions` endpoint (despite the configmap having an ASR model ID)

## Layer-by-Layer Mapping

### Endpoint 1: `/v1/audio/speech` (Standard TTS)

This is the primary endpoint. It is OpenAI-compatible in shape but requires custom param passthrough.

**Current failure**: Registered as `hosted_vllm/omnivoice` with `mode: chat`. The `hosted_vllm` provider is in `openai_compatible_providers` but NOT in `_CUSTOM_AUDIO_HANDLER_PROVIDERS`, so TTS routes through the OpenAI SDK path which (a) requires an API key, (b) doesn't forward custom params, (c) `drop_params: true` strips them anyway.

| Layer | Existing infra | OmniVoice need | Action |
|---|---|---|---|
| 1. Proxy route | `@router.post("/v1/audio/speech")` exists | Reuse | None |
| 2. Route dispatcher | `route_type="aspeech"` exists | Reuse | None |
| 3. Router method | `Router.aspeech()` exists | Reuse | None |
| 4. SDK function | `speech()` in main.py:7735 | Add `elif custom_llm_provider == "omnivoice"` branch | **Modify main.py** |
| 5. HTTP handler | `text_to_speech_handler()` exists | Reuse | None |
| 6. Provider config | None for omnivoice | Create `OmniVoiceTextToSpeechConfig` | **Create new file** |
| 7. Response | `StreamingResponse` exists | Reuse | None |

**Registration points**:
- `LlmProviders.OMNIVOICE` in `litellm/types/utils.py`
- `"omnivoice"` in `openai_compatible_providers` in `litellm/constants.py`
- `"omnivoice"` in `_CUSTOM_AUDIO_HANDLER_PROVIDERS` in `litellm/main.py`
- `ProviderConfigManager.get_provider_text_to_speech_config()` in `litellm/utils.py`

**Config class design** (following inception pattern since OmniVoice is also OpenAI-shaped):

```
OmniVoiceTextToSpeechConfig(OmniVoiceModelInfo, BaseTextToSpeechConfig)
    get_supported_openai_params() -> ["voice", "response_format", "speed", "language", "stream"]
    map_openai_params() -> resolve voice, collect passthrough, strip instructions
    validate_environment() -> no-op (no API key needed)
    get_complete_url() -> strip /v1, append /v1/audio/speech
    transform_text_to_speech_request() -> {model, input, voice or "alloy"} + passthrough
    transform_text_to_speech_response() -> HttpxBinaryResponseContent
```

### Endpoint 2: `/v1/audio/speech/clone` (One-shot Voice Cloning)

This is a non-standard endpoint. It accepts multipart form data with a reference audio file and synthesizes speech in the cloned voice.

**Comparison with hamsa voice cloning**: Hamsa uses a two-step protocol (extract tokens, then register) via the `/v1/audio/voices` first-class endpoint. OmniVoice uses a simpler one-shot approach: upload reference audio + text, get speech output directly.

| Layer | Existing infra | OmniVoice need | Action |
|---|---|---|---|
| 1. Proxy route | No route for `/v1/audio/speech/clone` | Need route or pass-through | **Add proxy route** |
| 2. Route dispatcher | No `route_type` for clone | Need new type or pass-through | **Add route type** |
| 3. Router method | No clone method | Need new or pass-through | **Add router method** |
| 4. SDK function | No clone function | Need new or pass-through | **Add SDK function** |
| 5. HTTP handler | No clone handler | Need new or pass-through | **Add handler** |
| 6. Provider config | No clone config | Need `OmniVoiceVoiceCloneConfig` | **Create config** |
| 7. Response | Binary audio stream | Reuse StreamingResponse | None |

This endpoint requires the full 7-layer treatment because:
- It accepts multipart form data (ref_audio file upload), which the standard TTS JSON path cannot handle
- The response is binary audio (same as TTS), not a JSON voice profile
- The URL path is non-standard (`/v1/audio/speech/clone`)

**Design decision**: Add as a first-class endpoint `/v1/audio/speech/clone` with a new `CallTypes.acreate_voice_clone` type. This mirrors how hamsa's voice cloning got first-class treatment. The alternative (pass-through endpoint) would bypass litellm's auth, logging, and cost tracking.

### Endpoint 3: `/v1/audio/script` (Multi-speaker Script Synthesis)

Non-standard endpoint for synthesizing multi-speaker dialogue from a script.

| Layer | Existing infra | OmniVoice need | Action |
|---|---|---|---|
| 1. Proxy route | No route for `/v1/audio/script` | Need route or pass-through | **Add proxy route** |
| 2-7 | No existing infra | Full chain needed | **Add full chain** |

This endpoint is lower priority. It could be handled via pass-through initially and promoted to first-class later if needed.

### Endpoint 4: `/v1/voices` (List Voices)

Non-standard GET endpoint returning available voices.

| Layer | Existing infra | OmniVoice need | Action |
|---|---|---|---|
| 1. Proxy route | No GET route for `/v1/voices` | Need route | **Add proxy route** |
| 2-7 | No existing infra | Full chain needed | **Add full chain** |

This is a simple GET with no body. Could be pass-through initially.

### Endpoint 5: `/v1/voices/profiles` (Voice Profile CRUD)

Non-standard CRUD for voice cloning profile management.

| Layer | Existing infra | OmniVoice need | Action |
|---|---|---|---|
| 1. Proxy route | No routes for `/v1/voices/profiles` | Need routes | **Add proxy routes** |
| 2-7 | No existing infra | Full chain needed | **Add full chain** |

Lower priority. Could be pass-through initially.

## Provider Registration Summary

### Files to create

```
litellm/llms/omnivoice/
    __init__.py                           # Re-exports
    common_utils.py                       # OmniVoiceModelInfo, OMNIVOICE_INTERNAL_PARAMS
    text_to_speech/
        __init__.py                       # Re-exports
        transformation.py                 # OmniVoiceTextToSpeechConfig
    voice/
        __init__.py                       # Re-exports
        transformation.py                 # OmniVoiceVoiceCloneConfig (for /v1/audio/speech/clone)
```

### Files to modify

| File | Change |
|---|---|
| `litellm/types/utils.py` | Add `OMNIVOICE = "omnivoice"` to `LlmProviders` enum. Add `CallTypes.acreate_voice_clone` and `acreate_script_speech` if needed. Add route mappings |
| `litellm/constants.py` | Add `"omnivoice"` to `openai_compatible_providers` |
| `litellm/main.py` | Add `"omnivoice"` to `_CUSTOM_AUDIO_HANDLER_PROVIDERS`. Add `elif custom_llm_provider == "omnivoice"` in `speech()`. Add `create_voice_clone()` / `acreate_voice_clone()` if implementing clone |
| `litellm/utils.py` | Add OMNIVOICE case to `get_provider_text_to_speech_config()` and `get_provider_voice_clone_config()` |
| `litellm/router.py` | Add `acreate_voice_clone()` / `_acreate_voice_clone()` if implementing clone |
| `litellm/proxy/route_llm_request.py` | Add `"acreate_voice_clone"` to route_type Literal and ROUTE_ENDPOINT_MAPPING |
| `litellm/proxy/proxy_server.py` | Add `@router.post("/v1/audio/speech/clone")` route |
| `oicm-litellm-layer/controller/models.py` | Update `detect_provider()` to return `"omnivoice"` for k2-fsa owned models |

## Implementation Priority

1. **TTS (`/v1/audio/speech`)**: Critical. This is the primary use case. Without this, TTS through the gateway is completely broken. Follows the inception pattern exactly.
2. **Voice clone (`/v1/audio/speech/clone`)**: Important. OmniVoice's one-shot voice cloning is a key differentiator. Requires first-class endpoint because of multipart file upload.
3. **Voices list (`/v1/voices`)** and **profiles CRUD**: Can be pass-through initially.
4. **Script synthesis (`/v1/audio/script`)**: Can be pass-through initially.

## Discovery Controller Integration

The `detect_provider()` function in `oicm-litellm-layer/controller/models.py` must recognize OmniVoice. The pod's `/v1/models` returns `owned_by: "k2-fsa"`. Two options:

1. Match on `owned_by == "k2-fsa"` (fragile if k2-fsa ships other model types)
2. Match on `model_id` containing "omnivoice" (more specific)

The current code only knows `"inception"` and `"hamsa"`:
```python
def detect_provider(owned_by: str, model_id: str) -> str:
    if "inception" in owned_by.lower():
        return "inception"
    if "hamsa" in owned_by.lower():
        return "hamsa"
    return "hosted_vllm"
```

Add:
```python
    if "omnivoice" in model_id.lower() or "k2-fsa" in owned_by.lower():
        return "omnivoice"
```

This ensures the model gets registered as `omnivoice/omnivoice` instead of `hosted_vllm/omnivoice`, routing TTS through the custom handler.

## Architecture Comparison: Inception vs Hamsa vs OmniVoice

| Aspect | Inception | Hamsa | OmniVoice |
|---|---|---|---|
| API shape | OpenAI-compatible | Custom | OpenAI-compatible + extensions |
| TTS path | `/v1/audio/speech` | `/tts/stream` | `/v1/audio/speech` |
| TTS body | `{model, input, voice}` + passthrough | `{text, speaker, language_id}` | `{model, input, voice}` + passthrough |
| STT | `/v1/audio/transcriptions` | `/transcribe` | Not served (configmap has ASR but no endpoint) |
| Voice cloning | Not supported | Two-step: `/tts/voice_clone` + `/tts/load_voice_cloning` | One-shot: `/v1/audio/speech/clone` (multipart) |
| Auth | None (no API key) | `x-api-key` header | None (no API key) |
| In `openai_compatible_providers` | Yes | No | Yes |
| In `_CUSTOM_AUDIO_HANDLER_PROVIDERS` | Yes | N/A (not in openai_compatible) | Yes |
| Pattern source for TTS config | N/A (is the pattern) | Custom | Follows inception |
