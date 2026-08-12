# Custom Providers

This section documents the custom LLM providers integrated into the LiteLLM proxy for the OICM platform. Each provider has its own API surface, authentication scheme, and request/response shaping that differs from the standard OpenAI-compatible path.

## Providers

### Inception

Inception Labs diffusion-based LLMs (Mercury series). Supports chat completions, text completions (FIM), text-to-speech, and speech-to-text. Uses OpenAI-compatible API shapes (`/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/chat/completions`, `/v1/completions`) but requires custom handler dispatch in `litellm/main.py` because router-level kwargs (e.g. `use_in_pass_through`) must be stripped before the request reaches the upstream pod.

Key implementation files:
- `litellm/llms/inception/common_utils.py` — `InceptionAudioModelInfo`, `INCEPTION_INTERNAL_PARAMS`
- `litellm/llms/inception/text_to_speech/transformation.py` — `InceptionTextToSpeechConfig`
- `litellm/llms/inception/transcription/transformation.py` — `InceptionAudioTranscriptionConfig`
- `litellm/llms/inception/chat/transformation.py` — `InceptionChatConfig`
- `litellm/llms/inception/completion/transformation.py` — `InceptionTextCompletionConfig`

### Hamsa

Hamsa Arabic/English TTS and STT with voice cloning and realtime WebSocket support. Uses a custom API surface (`/tts/stream`, `/transcribe`, `/tts/voice_clone`, `/ws`) with `x-api-key` header authentication. Not in `openai_compatible_providers`, so all endpoints use explicit provider branches in `litellm/main.py`.

Key implementation files:
- `litellm/llms/hamsa/common_utils.py` — `HamsaModelInfo`, `HAMSA_INTERNAL_PARAMS`
- `litellm/llms/hamsa/text_to_speech/transformation.py` — `HamsaTextToSpeechConfig`
- `litellm/llms/hamsa/transcription/transformation.py` — `HamsaAudioTranscriptionConfig`
- `litellm/llms/hamsa/voice/transformation.py` — `HamsaVoiceConfig`
- `litellm/llms/hamsa/realtime/handler.py` — `HamsaRealtimeConfig`, `hamsa_realtime`

### OmniVoice

OmniVoice is a k2-fsa OpenAI-compatible TTS server with one-shot voice cloning. Uses OpenAI-compatible API shapes (`/v1/audio/speech`) for standard TTS plus a multipart form-data endpoint (`/v1/audio/speech/clone`) for voice cloning. No API key required. In both `openai_compatible_providers` and `_CUSTOM_AUDIO_HANDLER_PROVIDERS`. The `speech()` function in `litellm/main.py` detects `ref_audio` in kwargs to switch between standard TTS (JSON body) and voice clone (multipart form-data) configs.

Key implementation files:
- `litellm/llms/omnivoice/common_utils.py` — `OmniVoiceModelInfo`, `OMNIVOICE_INTERNAL_PARAMS`
- `litellm/llms/omnivoice/text_to_speech/transformation.py` — `OmniVoiceTextToSpeechConfig`
- `litellm/llms/omnivoice/voice/transformation.py` — `OmniVoiceVoiceCloneConfig`
- `litellm/proxy/proxy_server.py` — `audio_speech_clone` route for `/v1/audio/speech/clone`
- `oicm-litellm-layer/controller/models.py` — `detect_provider()` detects `k2-fsa`/`k2fsa` as omnivoice

## Documents

| Document | Description |
|---|---|
| [LITELLM_ENDPOINT_ARCHITECTURE.md](LITELLM_ENDPOINT_ARCHITECTURE.md) | Full 7-layer trace of how LiteLLM implements endpoints and the pattern for adding a new custom provider endpoint |
| [GATEWAY_GUIDE.md](GATEWAY_GUIDE.md) | End-user guide for accessing Hamsa TTS/STT/voice through the LiteLLM gateway |
| [HAMSA_RESEARCH.md](HAMSA_RESEARCH.md) | Hamsa STT source code crawl, protocol analysis, and OpenAI compatibility assessment |
| [HAMSA_TTS_BEHAVIOR.md](HAMSA_TTS_BEHAVIOR.md) | Hamsa TTS pod source code analysis covering model loading, inference flow, and API endpoints |
| [inception-hamsa-audit.md](inception-hamsa-audit.md) | Logic mapping and code smell audit comparing inception and hamsa implementations |
| [inception-gateway-pod-parity.md](inception-gateway-pod-parity.md) | Request/response shape parity check: gateway vs pod-direct for inception TTS and STT |
| [INCEPTION_TTS_STT_GUIDE.md](INCEPTION_TTS_STT_GUIDE.md) | Sample curl commands and response shapes for calling inception TTS and STT through the gateway |
| [OMNIVOICE_LOGIC_MAPPING.md](OMNIVOICE_LOGIC_MAPPING.md) | Endpoint-by-endpoint analysis of OmniVoice API mapped to LiteLLM 7-layer architecture |
| [OMNIVOICE_TTS_GUIDE.md](OMNIVOICE_TTS_GUIDE.md) | Sample curl commands and response shapes for calling OmniVoice TTS and voice cloning through the gateway |

## Architecture Comparison

| Aspect | Inception | Hamsa | OmniVoice |
|---|---|---|---|
| Chat completions | OpenAI-compatible (`InceptionChatConfig`) | Not supported | Not supported |
| Text completion (FIM) | `InceptionTextCompletionConfig` + `_complete_text_completion_inception` | Not supported | Not supported |
| TTS | `/v1/audio/speech` (OpenAI-shaped) | `/tts/stream` (custom: `text`/`speaker`/`language_id`) | `/v1/audio/speech` (OpenAI-shaped) |
| STT | `/v1/audio/transcriptions` (multipart form) | `/transcribe` (JSON with base64 audio) | Not supported |
| Voice cloning | Not supported | `/tts/voice_clone` + `/tts/load_voice_clinking` (two-step JSON) | `/v1/audio/speech/clone` (one-shot multipart) |
| Realtime | Not supported | WebSocket `/ws` with handshake key injection | Not supported |
| Auth | No header injection (relies on OpenAI client key passing) | `x-api-key` header via `_inject_auth_headers` | No API key required |
| In `openai_compatible_providers` | Yes | No | Yes |
| In `_CUSTOM_AUDIO_HANDLER_PROVIDERS` | Yes | No | Yes |
| In `constants.py` | Yes (4 occurrences) | No | Yes |
| TTS dispatch in `speech()` | Explicit `elif` branch | Explicit `elif` branch | Explicit `elif` branch (config switches on `ref_audio`) |
| STT dispatch in `transcription()` | Generic `provider_config is not None` catch-all | Generic `provider_config is not None` catch-all | N/A |
| Custom proxy route | No | No | `/v1/audio/speech/clone` (multipart form-data) |
