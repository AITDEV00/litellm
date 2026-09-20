# Inception vs Hamsa: Logic Map & Code Smell Audit

> **Technique**: [Logic Mapping](../techniques/logic_mapping_technique.md) + [Code Smell Detection](../techniques/code_smell_detection_technique.md)
>
> **Scope**: `litellm/llms/inception/` and `litellm/llms/hamsa/` (all Python files), plus dispatch sites in `litellm/main.py`
>
> **Date**: 2025-07

---

## Part 1: Logic Map

### 1.1 Package Structure

```
litellm/llms/inception/
├── __init__.py                              # Re-exports: InceptionAudioModelInfo, InceptionTextToSpeechConfig, InceptionAudioTranscriptionConfig
├── common_utils.py                          # INCEPTION_INTERNAL_PARAMS, InceptionAudioModelInfo(BaseLLMModelInfo)
├── chat/
│   ├── __init__.py                          # Empty
│   └── transformation.py                    # InceptionChatConfig(OpenAILikeChatConfig)
├── completion/
│   ├── __init__.py                          # Empty
│   └── transformation.py                    # InceptionTextCompletionConfig(OpenAITextCompletionConfig)
├── text_to_speech/
│   ├── __init__.py                          # Re-exports: InceptionTextToSpeechConfig
│   └── transformation.py                    # InceptionTextToSpeechConfig(InceptionAudioModelInfo, BaseTextToSpeechConfig)
└── transcription/
    ├── __init__.py                          # Re-exports: InceptionAudioTranscriptionConfig
    └── transformation.py                    # InceptionAudioTranscriptionConfig(InceptionAudioModelInfo, BaseAudioTranscriptionConfig)

litellm/llms/hamsa/
├── __init__.py                              # Re-exports: HamsaModelInfo, HamsaTextToSpeechConfig, HamsaVoiceConfig
├── common_utils.py                          # HAMSA_INTERNAL_PARAMS, HamsaModelInfo(BaseLLMModelInfo)
├── text_to_speech/
│   ├── __init__.py                          # Re-exports: HamsaTextToSpeechConfig
│   └── transformation.py                    # HamsaTextToSpeechConfig(HamsaModelInfo, BaseTextToSpeechConfig)
├── transcription/
│   ├── __init__.py                          # Re-exports: HamsaAudioTranscriptionConfig
│   └── transformation.py                    # HamsaAudioTranscriptionConfig(HamsaModelInfo, BaseAudioTranscriptionConfig)
├── voice/
│   ├── __init__.py                          # Re-exports: HamsaVoiceConfig
│   └── transformation.py                    # HamsaVoiceConfig(HamsaModelInfo, BaseVoiceConfig)
└── realtime/
    ├── __init__.py                          # Re-exports: HamsaRealtimeConfig, hamsa_realtime
    └── handler.py                           # HamsaRealtimeConfig(HamsaModelInfo, BaseRealtimeConfig) + hamsa_realtime()
```

### 1.2 Capability Matrix

| Capability | Inception | Hamsa |
|---|---|---|
| Chat completions | `InceptionChatConfig` (OpenAI-compatible) | Not supported |
| Text completion (FIM) | `InceptionTextCompletionConfig` + `_complete_text_completion_inception` | Not supported |
| TTS | `InceptionTextToSpeechConfig` | `HamsaTextToSpeechConfig` |
| STT | `InceptionAudioTranscriptionConfig` | `HamsaAudioTranscriptionConfig` |
| Voice cloning | Not supported | `HamsaVoiceConfig` |
| Realtime | Not supported | `HamsaRealtimeConfig` + `hamsa_realtime()` |

### 1.3 TTS Flow (speech / aspeech)

```
main.py:7735  speech(model, input, voice, ...)
    │
    ├── get_llm_provider(model, custom_llm_provider, api_base)
    │   → resolves provider from model prefix (e.g. "inception/..." → "inception")
    │
    ├── ProviderConfigManager.get_provider_text_to_speech_config(provider)
    │   ├── utils.py:8163  HAMSA → HamsaTextToSpeechConfig()
    │   └── utils.py:8171  INCEPTION → InceptionTextToSpeechConfig()
    │
    ├── provider_config.map_openai_params(model, optional_params, voice, kwargs)
    │   ├── [INCEPTION] text_to_speech/transformation.py:46
    │   │   ├── _resolve_voice(voice) → str|None
    │   │   ├── pop _TTS_FORM_KEYS from optional_params → mapped_params
    │   │   ├── pop "instructions" (discarded)
    │   │   ├── pop "extra_body" → merge into mapped_params
    │   │   ├── _collect_passthrough(kwargs, mapped_params)
    │   │   └── _collect_passthrough(optional_params, mapped_params)
    │   │
    │   └── [HAMSA] text_to_speech/transformation.py:17
    │       ├── resolve speaker from voice (str/dict/other)
    │       ├── raise BaseLLMException(400) if speaker is None
    │       ├── pop "response_format" → map to mulaw bool
    │       ├── pop "speed" → mapped_params["speed"]
    │       ├── pop "instructions" (discarded)
    │       ├── pop "extra_body" → merge into mapped_params
    │       ├── passthrough kwargs (excluding HAMSA_INTERNAL_PARAMS)
    │       └── passthrough optional_params (excluding HAMSA_INTERNAL_PARAMS)
    │
    ├── [BRANCH: OpenAI SDK path]  main.py:7819
    │   if provider == "openai" OR (in openai_compatible_providers AND NOT in _CUSTOM_AUDIO_HANDLER_PROVIDERS)
    │   ├── inception: EXCLUDED (in _CUSTOM_AUDIO_HANDLER_PROVIDERS) → falls through
    │   └── hamsa: EXCLUDED (not in openai_compatible_providers) → falls through
    │
    ├── [BRANCH: hamsa]  main.py:8163
    │   elif custom_llm_provider == "hamsa":
    │   ├── import HamsaTextToSpeechConfig (lazy)
    │   ├── cast to hamsa_config
    │   ├── set litellm_params_dict["api_base"], ["api_key"] if provided
    │   └── base_llm_http_handler.text_to_speech_handler(...)
    │
    └── [BRANCH: inception]  main.py:8192
        elif custom_llm_provider == "inception":
        ├── import InceptionTextToSpeechConfig (lazy)
        ├── cast to inception_config
        ├── set litellm_params_dict["api_base"], ["api_key"] if provided
        └── base_llm_http_handler.text_to_speech_handler(...)
```

Inside `text_to_speech_handler` (base_llm_http_handler):

```
text_to_speech_handler(model, input, voice, provider_config, optional_params, ...)
    │
    ├── provider_config.validate_environment(headers, model, api_key, api_base)
    │   ├── [INCEPTION] returns headers unchanged (no auth injection)
    │   └── [HAMSA] calls _inject_auth_headers(headers, api_key)
    │       ├── resolves key via get_api_key (env HAMSA_API_KEY or passed)
    │       ├── raises BaseLLMException(401) if key missing
    │       ├── sets headers["x-api-key"] = resolved_key
    │       └── sets headers["Content-Type"] = "application/json"
    │
    ├── provider_config.get_complete_url(model, api_base, litellm_params)
    │   ├── [INCEPTION] _resolve_base(api_base) → strip trailing /v1 → + "/v1/audio/speech"
    │   └── [HAMSA] _resolve_base(api_base) + "/tts/stream"
    │
    ├── provider_config.transform_text_to_speech_request(model, input, voice, optional_params, ...)
    │   ├── [INCEPTION] builds {"model", "input", "voice": voice or "alloy"}
    │   │   ├── pop _TTS_FORM_KEYS into request_body
    │   │   └── _collect_passthrough(optional_params, request_body)
    │   │
    │   └── [HAMSA] builds {"text": input, "speaker": voice, "language_id": "ar", "stream": False}
    │       ├── conditionally add mulaw, dialect, expressiveness, speed, lang
    │       └── passthrough remaining (excluding HAMSA_INTERNAL_PARAMS)
    │
    ├── HTTP POST to provider URL with headers + body
    │
    └── provider_config.transform_text_to_speech_response(model, raw_response, logging_obj)
        ├── [INCEPTION] wraps in HttpxBinaryResponseContent(raw_response)
        └── [HAMSA] wraps in HttpxBinaryResponseContent(raw_response)
```

### 1.4 STT Flow (transcription / atranscription)

```
main.py:7451  transcription(model, file, ...)
    │
    ├── get_llm_provider(model, custom_llm_provider, api_base)
    │
    ├── ProviderConfigManager.get_provider_audio_transcription_config(provider)
    │   ├── utils.py:8975  HAMSA → HamsaAudioTranscriptionConfig()
    │   └── utils.py:8981  INCEPTION → InceptionAudioTranscriptionConfig()
    │
    ├── provider_config.map_openai_params(non_default_params, optional_params, model, drop_params)
    │   ├── [INCEPTION] transcription/transformation.py:36
    │   │   ├── copy known keys (language, prompt, response_format, temperature)
    │   │   ├── copy timestamp_granularities if present
    │   │   └── passthrough unknowns (excluding INCEPTION_INTERNAL_PARAMS + stt_known_keys)
    │   │
    │   └── [HAMSA] transcription/transformation.py:28
    │       ├── map language → lang
    │       ├── copy prompt
    │       └── passthrough unknowns (excluding HAMSA_INTERNAL_PARAMS + language/prompt)
    │
    ├── [BRANCH: OpenAI SDK path]  main.py:7577
    │   if provider == "openai" OR (in openai_compatible_providers AND NOT in _CUSTOM_AUDIO_HANDLER_PROVIDERS)
    │   ├── inception: EXCLUDED → falls through
    │   └── hamsa: EXCLUDED → falls through
    │
    └── [BRANCH: generic catch-all]  main.py:7653
        elif provider_config is not None:
        └── base_llm_http_handler.audio_transcriptions(
                model, audio_file, optional_params, ..., provider_config=provider_config)
```

Inside `audio_transcriptions` (base_llm_http_handler):

```
audio_transcriptions(model, audio_file, optional_params, ..., provider_config)
    │
    ├── provider_config.validate_environment(headers, model, messages, optional_params, litellm_params, api_key, api_base)
    │   ├── [INCEPTION] returns headers unchanged
    │   └── [HAMSA] calls _inject_auth_headers(headers, api_key)
    │
    ├── provider_config.get_complete_url(api_base, api_key, model, optional_params, litellm_params, stream)
    │   ├── [INCEPTION] _resolve_base(api_base) → strip trailing /v1 → + "/v1/audio/transcriptions"
    │   └── [HAMSA] _resolve_base(api_base) + "/transcribe"
    │
    ├── provider_config.transform_audio_transcription_request(model, audio_file, optional_params, litellm_params)
    │   ├── [INCEPTION] multipart form:
    │   │   ├── process_audio_file(audio_file) → filename, content, content_type
    │   │   ├── form_fields = {"model": model} + passthrough (excluding INCEPTION_INTERNAL_PARAMS)
    │   │   └── files = {"file": (filename, content, content_type)}
    │   │
    │   └── [HAMSA] JSON body with base64 audio:
    │       ├── process_audio_file(audio_file) → content
    │       ├── audio_b64 = base64.b64encode(content)
    │       ├── body = {"audio": audio_b64} + passthrough (excluding HAMSA_INTERNAL_PARAMS)
    │       └── json_bytes = json.dumps(body) → content_type="application/json"
    │
    ├── HTTP POST to provider URL
    │
    └── provider_config.transform_audio_transcription_response(raw_response)
        ├── [INCEPTION] extract text, set task="transcribe", copy extras, store payload in _hidden_params
        └── [HAMSA] extract text, set task="transcribe", copy extras, store payload in _hidden_params
```

### 1.5 Constants and Dispatch Registration

```
constants.py:
    openai_compatible_providers (line 722-774)
        ├── "inception" IS listed (line 774)
        └── "hamsa" is NOT listed

    provider_list
        ├── "inception" (line 547)
        ├── "text-completion-inception" (line 509)
        └── "hamsa" is NOT listed in constants.py at all

main.py:
    _CUSTOM_AUDIO_HANDLER_PROVIDERS (line 7731)
        = frozenset({"inception"})
        ├── inception IS in this set (excludes from OpenAI SDK path in both speech() and transcription())
        └── hamsa is NOT in this set (excluded from OpenAI SDK path simply because not in openai_compatible_providers)

LlmProviders enum (types/utils.py):
    INCEPTION = "inception" (line 3298)
    TEXT_COMPLETION_INCEPTION = "text-completion-inception" (line 3299)
    HAMSA = "hamsa" (line 3384, last member)

ProviderConfigManager (utils.py):
    Chat config:       INCEPTION registered (line 7622), HAMSA not registered
    TTS config:        HAMSA (line 8163), INCEPTION (line 8171)
    STT config:        HAMSA (line 8975), INCEPTION (line 8981)
    Voice config:      HAMSA only (line 9023)
```

### 1.6 Authentication Flow

```
[INCEPTION]
    validate_environment() → returns headers unchanged
    Auth relies on OpenAI client passing api_key as Bearer token
    get_api_key() returns api_key or "no-api-key-required"
    No env var lookup (INCEPTION_API_KEY only used in chat config, not audio)

[HAMSA]
    validate_environment() → calls _inject_auth_headers(headers, api_key)
    _inject_auth_headers():
        ├── resolved_key = get_api_key(api_key)  # api_key or os.environ["HAMSA_API_KEY"]
        ├── raises BaseLLMException(401) if key is None
        ├── headers["x-api-key"] = resolved_key
        └── headers["Content-Type"] = "application/json"
```

### 1.7 URL Construction

```
[INCEPTION TTS]
    _resolve_base(api_base) → strip trailing "/" → base
    if base ends with "/v1" (case-insensitive): base = base[:-3]
    return base + "/v1/audio/speech"

[INCEPTION STT]
    _resolve_base(api_base) → strip trailing "/" → base
    if base ends with "/v1" (case-insensitive): base = base[:-3]
    return base + "/v1/audio/transcriptions"

[HAMSA TTS]
    _resolve_base(api_base) → strip trailing "/" → base
    return base + "/tts/stream"

[HAMSA STT]
    _resolve_base(api_base) → strip trailing "/" → base
    return base + "/transcribe"

[HAMSA Voice]
    _resolve_base(api_base) → strip trailing "/" → base
    if action == "load": return base + "/tts/load_voice_cloning"
    else: return base + "/tts/voice_clone"

[HAMSA Realtime]
    _resolve_base(api_base) → strip trailing "/" → base
    https:// → wss://, http:// → ws://
    return base + "/ws"
```

---

## Part 2: Code Smell Audit

### L1: Automated Tool Baseline

| Tool | Provider | Result |
|---|---|---|
| pyflakes | inception | Clean (0 findings) |
| pyflakes | hamsa | Clean (0 findings) |
| ruff (F-category) | inception | All checks passed |
| ruff (F-category) | hamsa | All checks passed |
| ruff (full project config) | inception | All checks passed |
| ruff (full project config) | hamsa | All checks passed |
| ruff (strict, no project config) | inception | 2 PLC0415 (lazy imports inside functions) |
| ruff (strict, no project config) | hamsa | 8 findings: 4 PLC0415, 1 PLR0915, 1 PLR0912, 1 SIM, 1 RET |

**Note**: PLC0415 (import inside function) is in the project's `ruff.toml` external noqa list (line 11). The lazy imports are intentional to avoid circular import issues common in the litellm codebase.

**Assessment**: L1 is clean for both providers under the project's configured rules. No unused imports, no dead code detected by automated tools.

### L2: Per-File Semantic Checklist

#### inception/common_utils.py

- **[OK]** `INCEPTION_INTERNAL_PARAMS` is a frozenset (immutable). Good
- **[OK]** `InceptionAudioModelInfo` inherits `BaseLLMModelInfo`. Follows the standard pattern
- **[SMELL: LOW]** `get_api_key` returns `api_key or "no-api-key-required"` — a sentinel string rather than None. This is a design choice: inception pods don't require auth, so a placeholder is used. However, the string "no-api-key-required" could leak into headers if not handled downstream. In practice, `validate_environment` returns headers unchanged and the HTTP handler uses the api_key from litellm_params, so this sentinel is never sent as a real credential
- **[OK]** `_resolve_base` follows the same pattern as hamsa: raises `BaseLLMException(400)` if api_base is missing. The lazy import of `BaseLLMException` is intentional (avoids circular import)

#### inception/text_to_speech/transformation.py

- **[OK]** `_TTS_FORM_KEYS` is a tuple (immutable). Good
- **[OK]** `_resolve_voice` is a module-level function, not a method. Clean separation
- **[OK]** `_collect_passthrough` is a module-level function that filters against `INCEPTION_INTERNAL_PARAMS`. Reused in both `map_openai_params` and `transform_text_to_speech_request`. Good DRY
- **[SMELL: LOW]** `map_openai_params` pops keys from `optional_params` (mutating the input dict). This is the standard litellm pattern (hamsa does the same), but it violates immutability. Not a fix for this pass since it matches the codebase convention
- **[SMELL: LOW]** `transform_text_to_speech_request` pops `_TTS_FORM_KEYS` from `optional_params` again. By the time this is called, `map_openai_params` has already popped them. The second pop is defensive but technically dead code — the keys will always be None. Not harmful, but redundant
- **[OK]** `validate_environment` returns headers unchanged. Correct for inception (no auth injection needed; pods are internal)
- **[OK]** `get_complete_url` strips trailing `/v1` before appending `/v1/audio/speech`. This prevents the double-`/v1` bug that was fixed in commit 2ced4330a4
- **[OK]** `transform_text_to_speech_response` wraps in `HttpxBinaryResponseContent`. Matches hamsa

#### inception/transcription/transformation.py

- **[OK]** `map_openai_params` builds `stt_known_keys` as a local set. Clean
- **[OK]** `validate_environment` returns headers unchanged. Correct
- **[OK]** `get_complete_url` strips trailing `/v1` before appending `/v1/audio/transcriptions`. Matches TTS pattern
- **[OK]** `transform_audio_transcription_request` uses multipart form with `process_audio_file`. Standard OpenAI-compatible STT format
- **[OK]** `transform_audio_transcription_response` extracts text, sets task, copies extras, stores full payload. Clean

#### inception/chat/transformation.py

- **[OK]** `InceptionChatConfig` extends `OpenAILikeChatConfig`. Standard pattern for OpenAI-compatible chat
- **[OK]** `_get_openai_compatible_provider_info` resolves api_base and api_key with env var fallbacks
- **[SMELL: LOW]** `get_supported_openai_params` returns a hardcoded list including non-standard params like `"diffusing"` and `"realtime"`. These are inception-specific extension params. Acceptable since inception has unique diffusion capabilities

#### inception/completion/transformation.py

- **[OK]** `InceptionTextCompletionConfig` extends `OpenAITextCompletionConfig`. Standard
- **[OK]** `map_openai_params` maps `max_completion_tokens` → `max_tokens`. Clean

#### hamsa/common_utils.py

- **[OK]** `HAMSA_INTERNAL_PARAMS` is a frozenset. Good
- **[OK]** `HamsaModelInfo` inherits `BaseLLMModelInfo`. Follows the same pattern as inception
- **[OK]** `get_api_key` uses `os.environ.get("HAMSA_API_KEY")` as fallback. Standard env var pattern
- **[OK]** `get_api_base` uses `os.environ.get("HAMSA_API_BASE")` as fallback. No default URL (unlike inception which has `https://api.inceptionlabs.ai/v1`). Correct: hamsa pods are internal and must be explicitly configured
- **[OK]** `_resolve_base` follows the same pattern as inception. Clean
- **[OK]** `_inject_auth_headers` is a static method that resolves key, raises on missing, sets `x-api-key` and `Content-Type`. This is the key differentiator from inception: hamsa requires explicit auth
- **[SMELL: LOW]** `HAMSA_INTERNAL_PARAMS` includes the OpenAI audio params themselves (`"model"`, `"voice"`, `"response_format"`, `"speed"`, `"instructions"`, `"language"`, `"prompt"`, `"temperature"`, `"timestamp_granularities"`, `"stream"`). Inception's frozenset does NOT include these. This is intentional: hamsa's `map_openai_params` pops these explicitly before passthrough, so they're in the internal set to prevent re-injection. Inception's `_TTS_FORM_KEYS` serves the same purpose for TTS, but inception's STT uses a local `stt_known_keys` set instead. Different approaches to the same problem

#### hamsa/text_to_speech/transformation.py

- **[SMELL: MEDIUM]** `map_openai_params` has 15 branches (ruff PLR0912). The voice resolution, response_format mapping, speed mapping, instructions pop, extra_body merge, kwargs passthrough, and optional_params passthrough are all inline. Inception extracts `_resolve_voice` and `_collect_passthrough` as module-level functions, reducing method complexity. Hamsa should follow the same pattern
- **[SMELL: LOW]** Voice resolution logic (lines 26-33) is duplicated inline rather than extracted to a helper function. Inception has `_resolve_voice()` as a reusable module function. Hamsa inlines the same str/dict/other pattern
- **[SMELL: LOW]** `map_openai_params` raises `BaseLLMException(400)` if speaker is None. Inception's `_resolve_voice` returns None and lets `transform_text_to_speech_request` default to "alloy". Different design philosophies: hamsa requires explicit speaker, inception has a default. Both are valid for their respective APIs
- **[SMELL: LOW]** `transform_text_to_speech_request` uses a chain of `if "key" in optional_params: request_body[key] = optional_params.pop(key)` for mulaw, dialect, expressiveness, speed, lang. This is 5 repetitive blocks. Could be a loop over a tuple of known hamsa-specific keys, but the current approach is readable
- **[OK]** `validate_environment` calls `_inject_auth_headers`. Correct
- **[OK]** `get_complete_url` returns `base + "/tts/stream"`. No `/v1` stripping needed since hamsa doesn't use OpenAI-compatible paths
- **[OK]** `transform_text_to_speech_response` wraps in `HttpxBinaryResponseContent`. Matches inception

#### hamsa/transcription/transformation.py

- **[OK]** `map_openai_params` maps `language` → `lang`. Clean
- **[OK]** `validate_environment` calls `_inject_auth_headers`. Correct
- **[OK]** `get_complete_url` returns `base + "/transcribe"`. Clean
- **[OK]** `transform_audio_transcription_request` uses JSON body with base64 audio. Different from inception's multipart form, but correct for hamsa's API
- **[OK]** `transform_audio_transcription_response` matches inception's pattern exactly. Good consistency
- **[SMELL: LOW]** Uses `Union[dict, Headers]` instead of `dict | Headers` (Python 3.10+). Inception uses `dict | Headers`. Cosmetic inconsistency

#### hamsa/voice/transformation.py

- **[OK]** `_SPEAKER_ALIASES` and `_AUDIO_PATH_ALIASES` are frozensets. Good
- **[SMELL: LOW]** Class-level constants `_SPEAKER_ALIASES` and `_AUDIO_PATH_ALIASES` are defined in the middle of the class (after `transform_create_voice_request`, before `transform_create_voice_response`). Convention is to put constants at the top of the class. Minor readability issue
- **[OK]** `transform_create_voice_request` branches on `voice_action` ("load" vs "register"). Clean early-return pattern
- **[OK]** `transform_create_voice_response` uses `next()` with frozenset alias lookup. Clean

#### hamsa/realtime/handler.py

- **[SMELL: MEDIUM]** `hamsa_realtime` function has 59 statements (ruff PLR0915, threshold 50). The function handles WebSocket proxying with two nested async coroutines. Could be refactored to extract `forward_client_to_backend` and `forward_backend_to_client` as module-level functions taking explicit dependencies, but the current closure approach captures state cleanly
- **[SMELL: LOW]** `forward_backend_to_client` catches `Exception` and logs it. This is broad but acceptable for a WebSocket proxy that must survive protocol errors
- **[OK]** Handshake key injection is a clever security pattern: the API key is injected into the first handshake message rather than sent as a header. This matches hamsa's WebSocket protocol design
- **[OK]** `transform_realtime_response` handles bytes→str decoding, JSON parsing with fallback. Robust

### L3: Cross-Reference Analysis

#### INCEPTION_INTERNAL_PARAMS vs HAMSA_INTERNAL_PARAMS

| Param | In INCEPTION | In HAMSA | Notes |
|---|---|---|---|
| `model` | No | Yes | Hamsa strips it because `transform_text_to_speech_request` doesn't use the OpenAI `model` field (uses `text`/`speaker` instead). Inception passes `model` through to the request body |
| `voice` | No | Yes | Hamsa strips it because it maps to `speaker`. Inception passes `voice` through |
| `response_format` | No | Yes | Hamsa maps it to `mulaw` bool. Inception passes it through as-is |
| `speed` | No | Yes | Hamsa pops it explicitly. Inception passes it through |
| `instructions` | No | Yes | Both pop it in `map_openai_params`, but hamsa also has it in the internal set for double protection |
| `language` | No | Yes | Hamsa maps it to `language_id`/`lang`. Inception passes it through |
| `prompt` | No | Yes | Hamsa uses it for STT. Inception passes it through |
| `temperature` | No | Yes | Hamsa strips it. Inception passes it through for STT |
| `timestamp_granularities` | No | Yes | Hamsa strips it. Inception handles it in `map_openai_params` via `stt_known_keys` local set |
| `stream` | No | Yes | Hamsa hardcodes `stream: False` in TTS request. Inception passes it through |
| `atranscription` | Yes | No | Inception includes it. Hamsa doesn't (hamsa doesn't use the OpenAI SDK path that sets this flag) |
| All litellm_* / proxy_* / user_api_key_* | Yes | Yes | Both strip the same set of litellm internal kwargs |
| `use_in_pass_through` | Yes | Yes | Both include the 5 router-level params added in this PR |
| `use_litellm_proxy` | Yes | Yes | Same |
| `use_xai_oauth` | Yes | Yes | Same |
| `use_chat_completions_api` | Yes | Yes | Same |
| `merge_reasoning_content_in_choices` | Yes | Yes | Same |

**Assessment**: The difference in internal params is intentional and correct. Hamsa strips the OpenAI audio param names because it remaps them to its own field names. Inception keeps them because its API is OpenAI-compatible and accepts them as-is. The 5 router-level params (`use_in_pass_through`, etc.) are consistently present in both sets. This is the fix that resolved the HTTP 500 bug.

#### Enum ↔ Dispatch Cross-Check

| Enum Member | Chat Config | TTS Config | STT Config | Voice Config | Realtime |
|---|---|---|---|---|---|
| `INCEPTION` | `utils.py:7622` | `utils.py:8171` | `utils.py:8981` | N/A | N/A |
| `TEXT_COMPLETION_INCEPTION` | `main.py:5540` (FIM dispatch) | N/A | N/A | N/A | N/A |
| `HAMSA` | Not registered | `utils.py:8163` | `utils.py:8975` | `utils.py:9023` | Direct call from `main.py` |

**Assessment**: All enum members have corresponding dispatch entries. No orphaned enum values or missing dispatch table entries.

#### Constant ↔ Usage Cross-Check

| Constant | Defined In | Used In | Status |
|---|---|---|---|
| `INCEPTION_INTERNAL_PARAMS` | inception/common_utils.py:7 | text_to_speech/transformation.py, transcription/transformation.py | OK |
| `HAMSA_INTERNAL_PARAMS` | hamsa/common_utils.py:7 | text_to_speech/transformation.py, transcription/transformation.py | OK |
| `_TTS_FORM_KEYS` | inception/text_to_speech/transformation.py:14 | text_to_speech/transformation.py (map_openai_params, transform_text_to_speech_request) | OK |
| `_SPEAKER_ALIASES` | hamsa/voice/transformation.py:107 | voice/transformation.py (transform_create_voice_response) | OK |
| `_AUDIO_PATH_ALIASES` | hamsa/voice/transformation.py:108 | voice/transformation.py (transform_create_voice_response) | OK |
| `_CUSTOM_AUDIO_HANDLER_PROVIDERS` | main.py:7731 | main.py:7578 (transcription), main.py:7821 (speech) | OK |

#### Function ↔ Caller Cross-Check

| Function | Defined In | Called From | Status |
|---|---|---|---|
| `_resolve_voice` | inception/tts:16 | inception/tts:map_openai_params | OK |
| `_collect_passthrough` | inception/tts:23 | inception/tts:map_openai_params, transform_text_to_speech_request | OK |
| `_resolve_base` | inception/common_utils:80, hamsa/common_utils:84 | All get_complete_url methods in both providers | OK |
| `_inject_auth_headers` | hamsa/common_utils:104 | hamsa TTS/STT/voice/realtime validate_environment | OK |
| `hamsa_realtime` | hamsa/realtime/handler.py:65 | main.py (realtime dispatch) | OK |

#### Dispatch Pattern Consistency

```
speech() dispatch:
    ├── OpenAI SDK path (excludes inception via _CUSTOM_AUDIO_HANDLER_PROVIDERS, excludes hamsa via not-in-openai_compatible_providers)
    ├── elif hamsa → explicit branch (main.py:8163)
    └── elif inception → explicit branch (main.py:8192)
    → Both branches are structurally identical: import config, cast, set params, call text_to_speech_handler

transcription() dispatch:
    ├── OpenAI SDK path (same exclusion logic)
    └── elif provider_config is not None → generic catch-all (main.py:7653)
    → Both inception and hamsa fall through to the generic catch-all
    → This works because ProviderConfigManager resolves both configs
```

**[SMELL: MEDIUM]** Inconsistency: `speech()` has explicit `elif` branches for both hamsa and inception, but `transcription()` uses the generic catch-all for both. The TTS path requires explicit branches because `text_to_speech_provider_config` is resolved before the dispatch and passed into the OpenAI SDK branch. The STT path resolves `provider_config` before the dispatch too, so the generic catch-all works. But the asymmetry means:
1. If a new custom audio provider is added, it needs an explicit `elif` in `speech()` but not in `transcription()`
2. The `speech()` elif blocks are copy-paste identical (only the config class name differs), which is a DRY violation

### L4: Fix-Then-Recheck Results

No fixes were applied in this audit pass. The findings are documentation-level observations. The L1 tools are clean, and the L2/L3 findings are architectural patterns that match the broader litellm codebase conventions.

---

## Part 3: Comparison Assessment

### Are the implementations following the same pattern?

**Partially yes, with intentional divergences.**

Both providers follow the same structural pattern:
1. A `common_utils.py` with a `*INTERNAL_PARAMS` frozenset and a `*ModelInfo(BaseLLMModelInfo)` class
2. Per-endpoint `transformation.py` files with config classes inheriting from both the model info and the base config
3. The same method set: `get_supported_openai_params`, `map_openai_params`, `validate_environment`, `get_complete_url`, `transform_*_request`, `transform_*_response`
4. The same response wrapping pattern: `HttpxBinaryResponseContent` for TTS, `TranscriptionResponse` for STT

The divergences are intentional and driven by the upstream API shapes:
- Inception uses OpenAI-compatible paths (`/v1/audio/speech`, `/v1/audio/transcriptions`) and multipart form for STT
- Hamsa uses custom paths (`/tts/stream`, `/transcribe`) and JSON+base64 for STT
- Inception doesn't inject auth headers (internal pods, no auth required)
- Hamsa injects `x-api-key` via `_inject_auth_headers`

### Are both good implementations?

**Yes, with minor improvements possible.**

**Inception strengths:**
- Extracts `_resolve_voice` and `_collect_passthrough` as module-level functions (better DRY)
- `_TTS_FORM_KEYS` tuple centralizes the form key list
- Simpler `map_openai_params` due to helper extraction

**Inception weaknesses:**
- `transform_text_to_speech_request` re-pops `_TTS_FORM_KEYS` that were already popped in `map_openai_params` (redundant)
- `get_api_key` returns a sentinel string "no-api-key-required" instead of None (could confuse downstream consumers)

**Hamsa strengths:**
- `_inject_auth_headers` is a clean, reusable auth method used across all endpoints
- Voice cloning and realtime support show good extensibility
- `HAMSA_INTERNAL_PARAMS` includes the OpenAI audio param names, providing double protection against leakage

**Hamsa weaknesses:**
- `map_openai_params` in TTS has too many branches (15 > 12 threshold) and inlines voice resolution instead of extracting a helper
- Uses `Union[str, Dict]` instead of `str | dict` (older typing style, inconsistent with inception's modern `str | dict`)
- `transform_text_to_speech_request` uses repetitive `if "key" in optional_params: pop` blocks instead of a loop
- Class-level constants (`_SPEAKER_ALIASES`, `_AUDIO_PATH_ALIASES`) are placed in the middle of the class instead of at the top

### Recommendations (not fixed in this pass)

1. **Hamsa TTS**: Extract voice resolution to a module-level `_resolve_speaker()` function, matching inception's `_resolve_voice()` pattern. This would reduce branch count from 15 to within threshold
2. **Hamsa TTS**: Replace the 5 repetitive `if "key" in optional_params: pop` blocks with a loop over a `_HAMSA_TTS_KEYS` tuple
3. **Hamsa**: Migrate `Union[str, Dict]` to `str | dict` and `List` to `list` for consistency with inception and modern Python
4. **Hamsa voice**: Move `_SPEAKER_ALIASES` and `_AUDIO_PATH_ALIASES` to the top of the class
5. **Inception TTS**: Remove the redundant `_TTS_FORM_KEYS` pop in `transform_text_to_speech_request` (already popped in `map_openai_params`)
6. **main.py speech()**: The hamsa and inception elif blocks are copy-paste identical. Consider extracting a shared `_dispatch_custom_tts_provider(custom_llm_provider, config_class, ...)` helper to eliminate the duplication
7. **main.py**: Consider adding hamsa to `_CUSTOM_AUDIO_HANDLER_PROVIDERS` for consistency, even though it doesn't need to be excluded from the OpenAI SDK path (it's already excluded by not being in `openai_compatible_providers`). This would make the exclusion logic explicit rather than implicit

### Overall Verdict

Both implementations are functional, well-structured, and follow the litellm provider pattern. The inception implementation is slightly cleaner due to better helper extraction. The hamsa implementation is more feature-rich (voice cloning, realtime) but has higher complexity in `map_openai_params`. Neither has any L1 tool findings, and the L2/L3 findings are minor architectural observations, not bugs. The 5 router-level params (`use_in_pass_through`, `use_litellm_proxy`, `use_xai_oauth`, `use_chat_completions_api`, `merge_reasoning_content_in_choices`) are consistently present in both `INCEPTION_INTERNAL_PARAMS` and `HAMSA_INTERNAL_PARAMS`, which was the fix that resolved the HTTP 500 TTS bug.
