# OmniVoice TTS and Voice Cloning: Curl Guide

> **Scope**: How to call OmniVoice text-to-speech and one-shot voice cloning through the LiteLLM gateway, with sample curl commands and expected response shapes
>
> **Related docs**: [OMNIVOICE_LOGIC_MAPPING.md](../custom-providers/OMNIVOICE_LOGIC_MAPPING.md) (endpoint-by-endpoint architecture mapping), [INCEPTION_TTS_STT_GUIDE.md](INCEPTION_TTS_STT_GUIDE.md) (similar OpenAI-compatible TTS provider)

---

## Gateway endpoints

| Endpoint | Gateway URL |
|---|---|
| Text-to-speech | `https://litellm.ecouncil.ae/v1/audio/speech` |
| Voice cloning | `https://litellm.ecouncil.ae/v1/audio/speech/clone` |

Authentication is the standard LiteLLM gateway key passed as a Bearer token. The OmniVoice pod itself does not require auth (no API key), but the gateway always requires it:

```
Authorization: Bearer <your-api-key>
```

The gateway maps the user-facing model name to the internal pod model. The discovery controller registers the model as `omnivoice/omnivoice`, so you send `omnivoice` as the model and the gateway resolves the provider, api_base, and routing.

---

## Text-to-speech (TTS)

### Request shape

TTS uses an OpenAI-compatible JSON body posted to `/v1/audio/speech`. The gateway builds three core fields and forwards any additional recognized parameters:

| Field | Required | Default | Notes |
|---|---|---|---|
| `model` | yes | | User-facing name `omnivoice` |
| `input` | yes | | The text to synthesize |
| `voice` | no | `alloy` | Voice identifier; defaults to `alloy` if omitted or null |
| `response_format` | no | | Audio format (e.g. `mp3`, `wav`, `flac`) |
| `speed` | no | | Playback speed multiplier (0.25 to 4.0) |
| `language` | no | | Language code for synthesis |
| `stream` | no | | Whether to stream the audio |

OmniVoice also accepts custom inference parameters that are passed through verbatim:

| Custom param | Type | Range | Notes |
|---|---|---|---|
| `num_step` | int | 1-64 | Number of denoising steps |
| `guidance_scale` | float | 0-10 | Classifier-free guidance scale |
| `denoise` | bool | | Enable denoising |
| `t_shift` | float | 0-2 | Time shift |
| `position_temperature` | float | 0-10 | Position temperature |
| `class_temperature` | float | 0-2 | Class temperature |
| `duration` | float | 0.1-60 | Target audio duration in seconds |
| `layer_penalty_factor` | float | 0+ | Layer penalty factor |
| `preprocess_prompt` | bool | | Preprocess the prompt |
| `postprocess_output` | bool | | Postprocess the output |
| `audio_chunk_duration` | float | >0 | Audio chunk duration |
| `audio_chunk_threshold` | float | >0 | Audio chunk threshold |
| `request_timeout_s` | int | 1-600 | Request timeout in seconds |
| `speaker` | string | | Speaker identifier for multi-speaker models |

The `instructions` parameter (an OpenAI TTS param) is silently dropped because OmniVoice returns a 422 error when it receives it. LiteLLM-internal kwargs (things like `api_base`, `api_key`, `metadata`, `litellm_call_id`, etc.) are also stripped before the request reaches the upstream pod.

### Sample curl

```bash
curl -sS https://litellm.ecouncil.ae/v1/audio/speech \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "omnivoice",
    "input": "Hello world, this is a text-to-speech test.",
    "voice": "alloy",
    "response_format": "mp3"
  }' \
  -o tts_output.mp3
```

Save the binary response to a file because the response body is raw audio, not JSON.

### Minimal curl (relying on defaults)

```bash
curl -sS https://litellm.ecouncil.ae/v1/audio/speech \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "omnivoice", "input": "Hello world."}' \
  -o tts_output.mp3
```

With no `voice` supplied, the gateway defaults it to `alloy` before forwarding.

### Curl with custom inference params

```bash
curl -sS https://litellm.ecouncil.ae/v1/audio/speech \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "omnivoice",
    "input": "Hello world with custom params.",
    "voice": "alloy",
    "response_format": "wav",
    "speed": 1.0,
    "language": "ar",
    "num_step": 32,
    "guidance_scale": 3.0,
    "denoise": true
  }' \
  -o tts_custom.wav
```

### Local proxy (dev)

```bash
python litellm/proxy/proxy_cli.py --config litellm/proxy/dev_config.yaml --detailed_debug --reload --use_v2_migration_resolver 2>&1 | tee litellm.log
```

Then curl localhost:4000:

```bash
curl -sS http://localhost:4000/v1/audio/speech \
  -H "Authorization: Bearer {{ master_key }}" \
  -H "Content-Type: application/json" \
  -d '{"model": "omnivoice", "input": "Hello from local proxy."}' \
  -o tts_local.mp3
```

### Response shape

The response is binary audio. The key aspects:

| Aspect | Value |
|---|---|
| HTTP status | `200` on success |
| Content-Type | `audio/mpeg` (or requested format) |
| Body | Raw audio bytes |

---

## Voice cloning

### Request shape

Voice cloning uses multipart form-data posted to `/v1/audio/speech/clone`. This is a one-shot cloning endpoint: you provide a reference audio clip and the text to synthesize, and the pod generates speech in the voice of the reference clip. No separate voice registration step is needed (unlike Hamsa's two-step clone protocol).

| Field | Required | Default | Notes |
|---|---|---|---|
| `text` | yes | | The text to synthesize in the cloned voice |
| `ref_audio` | yes | | Reference audio file (multipart file upload) |
| `ref_text` | no | | Transcript of the reference audio (improves quality) |
| `response_format` | no | | Audio format (e.g. `mp3`, `wav`, `flac`) |
| `speed` | no | | Playback speed multiplier (0.25 to 4.0) |
| `stream` | no | | Whether to stream the audio |
| `language` | no | | Language code for synthesis |
| `num_step` | no | | Number of denoising steps (1-64) |
| `guidance_scale` | no | | Classifier-free guidance scale (0-10) |
| `denoise` | no | | Enable denoising |
| `t_shift` | no | | Time shift (0-2) |
| `position_temperature` | no | | Position temperature (0-10) |
| `class_temperature` | no | | Class temperature (0-2) |
| `duration` | no | | Target audio duration in seconds (0.1-60) |
| `layer_penalty_factor` | no | | Layer penalty factor (0+) |
| `preprocess_prompt` | no | | Preprocess the prompt |
| `postprocess_output` | no | | Postprocess the output |
| `audio_chunk_duration` | no | | Audio chunk duration (>0) |
| `audio_chunk_threshold` | no | | Audio chunk threshold (>0) |
| `request_timeout_s` | no | | Request timeout in seconds (1-600) |

The `voice` field is set to `"clone"` by the gateway route. It is not forwarded to the pod; it is only used internally for routing.

### Sample curl

```bash
curl -sS https://litellm.ecouncil.ae/v1/audio/speech/clone \
  -H "Authorization: Bearer <your-api-key>" \
  -F "text=Hello world, this is a cloned voice test." \
  -F "ref_audio=@reference.wav" \
  -F "ref_text=This is the reference audio transcript." \
  -F "response_format=mp3" \
  -F "num_step=32" \
  -F "guidance_scale=3.0" \
  -o clone_output.mp3
```

### Minimal curl (text + ref_audio only)

```bash
curl -sS https://litellm.ecouncil.ae/v1/audio/speech/clone \
  -H "Authorization: Bearer <your-api-key>" \
  -F "text=Hello world." \
  -F "ref_audio=@reference.wav" \
  -o clone_output.mp3
```

### Local proxy (dev)

```bash
curl -sS http://localhost:4000/v1/audio/speech/clone \
  -H "Authorization: Bearer {{ master_key }}" \
  -F "text=Hello from local proxy." \
  -F "ref_audio=@reference.wav" \
  -o clone_local.mp3
```

### Response shape

The response is binary audio, same as TTS:

| Aspect | Value |
|---|---|
| HTTP status | `200` on success |
| Content-Type | `audio/mpeg` (or requested format) |
| Body | Raw audio bytes |

---

## How it works internally

The `speech()` function in `litellm/main.py` checks whether `ref_audio` is present in the kwargs. If it is, the `OmniVoiceVoiceCloneConfig` is used, which builds multipart form-data with the reference audio file and text. If `ref_audio` is absent, the `OmniVoiceTextToSpeechConfig` is used, which builds a standard JSON body.

Both configs share the same `text_to_speech_handler` in `llm_http_handler.py`. The handler checks the `TextToSpeechRequestData` return: if `form_data` is present, it sends a multipart POST; if `dict_body` is present, it sends a JSON POST.

The `get_provider_text_to_speech_config()` in `litellm/utils.py` always returns `OmniVoiceTextToSpeechConfig` for the `OMNIVOICE` provider. The `speech()` function overrides this to `OmniVoiceVoiceCloneConfig` when `ref_audio` is detected, before calling the handler.

### Discovery controller registration

The OICM discovery controller detects OmniVoice pods by checking for `k2-fsa` or `k2fsa` in the model owner or ID. It registers the model with:

- `provider`: `omnivoice`
- `model_id`: `omnivoice`
- `api_base`: the pod's ClusterIP service URL (e.g. `http://10.43.173.29:8080`)
- `api_key`: empty string (no auth required)
- `mode`: `text_to_speech` (mapped to `chat` for litellm registration)
- `drop_params`: `True` (does not affect custom handler path because `map_openai_params` hardcodes `drop_params=False`)

---

## Differences from Hamsa voice cloning

| Aspect | OmniVoice | Hamsa |
|---|---|---|
| Protocol | One-shot: single multipart POST | Two-step: POST to register, then POST to synthesize |
| Endpoint | `/v1/audio/speech/clone` | `/tts/voice_clone` + `/tts/load_voice_clinking` |
| Auth | No API key | `x-api-key` header |
| Request format | Multipart form-data | JSON with base64 audio |
| Gateway route | Custom `/v1/audio/speech/clone` route | Pass-through or custom route |
