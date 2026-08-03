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

## Documents

| Document | Description |
|---|---|
| [LITELLM_ENDPOINT_ARCHITECTURE.md](LITELLM_ENDPOINT_ARCHITECTURE.md) | Full 7-layer trace of how LiteLLM implements endpoints and the pattern for adding a new custom provider endpoint |
| [GATEWAY_GUIDE.md](GATEWAY_GUIDE.md) | End-user guide for accessing Hamsa TTS/STT/voice through the LiteLLM gateway |
| [HAMSA_RESEARCH.md](HAMSA_RESEARCH.md) | Hamsa STT source code crawl, protocol analysis, and OpenAI compatibility assessment |
| [HAMSA_TTS_BEHAVIOR.md](HAMSA_TTS_BEHAVIOR.md) | Hamsa TTS pod source code analysis covering model loading, inference flow, and API endpoints |
| [inception-hamsa-audit.md](inception-hamsa-audit.md) | Logic mapping and code smell audit comparing inception and hamsa implementations |
| [inception-gateway-pod-parity.md](inception-gateway-pod-parity.md) | Request/response shape parity check: gateway vs pod-direct for inception TTS and STT |

## Architecture Comparison

| Aspect | Inception | Hamsa |
|---|---|---|
| Chat completions | OpenAI-compatible (`InceptionChatConfig`) | Not supported |
| Text completion (FIM) | `InceptionTextCompletionConfig` + `_complete_text_completion_inception` | Not supported |
| TTS | `/v1/audio/speech` (OpenAI-shaped) | `/tts/stream` (custom: `text`/`speaker`/`language_id`) |
| STT | `/v1/audio/transcriptions` (multipart form) | `/transcribe` (JSON with base64 audio) |
| Voice cloning | Not supported | `/tts/voice_clone` + `/tts/load_voice_clinking` |
| Realtime | Not supported | WebSocket `/ws` with handshake key injection |
| Auth | No header injection (relies on OpenAI client key passing) | `x-api-key` header via `_inject_auth_headers` |
| In `openai_compatible_providers` | Yes | No |
| In `_CUSTOM_AUDIO_HANDLER_PROVIDERS` | Yes | No |
| In `constants.py` | Yes (4 occurrences) | No |
| TTS dispatch in `speech()` | Explicit `elif` branch | Explicit `elif` branch |
| STT dispatch in `transcription()` | Generic `provider_config is not None` catch-all | Generic `provider_config is not None` catch-all |
