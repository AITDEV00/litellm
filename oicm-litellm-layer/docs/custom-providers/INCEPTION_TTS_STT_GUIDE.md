# Inception TTS and STT: Curl Guide

> **Scope**: How to call Inception text-to-speech and speech-to-text through the LiteLLM gateway, with sample curl commands and expected response shapes
>
> **Related docs**: [inception-gateway-pod-parity.md](inception-gateway-pod-parity.md) (request/response parity vs pod-direct), [inception-hamsa-audit.md](inception-hamsa-audit.md) (internal code flow)

---

## Gateway endpoints

| Endpoint | Gateway URL |
|---|---|
| Text-to-speech | `https://litellm.adeoaiengine.ecouncil.ae/v1/audio/speech` |
| Speech-to-text | `https://litellm.adeoaiengine.ecouncil.ae/v1/audio/transcriptions` |

Authentication is the standard LiteLLM gateway key passed as a Bearer token. The Inception pods themselves do not require auth, but the gateway always requires it:

```
Authorization: Bearer <your-api-key>
```

The gateway maps the user-facing model name to the internal pod model. For TTS, `inception-tts` resolves to `inception/inception-tts`. For STT, `inception-stt` resolves to `inception/inception-stt`. You send the short name; the gateway does the lookup.

---

## Text-to-speech (TTS)

### Request shape

TTS uses an OpenAI-compatible JSON body posted to `/v1/audio/speech`. The gateway builds three core fields and forwards any additional recognized parameters:

| Field | Required | Default | Notes |
|---|---|---|---|
| `model` | yes | | User-facing name `inception-tts` |
| `input` | yes | | The text to synthesize |
| `voice` | no | `alloy` | Voice identifier; defaults to `alloy` if omitted or null |
| `response_format` | no | | Audio format (e.g. `mp3`) |
| `speed` | no | | Playback speed multiplier |
| `language` | no | | Language code for synthesis |
| `stream` | no | | Whether to stream the audio |

Any other fields you pass that are not LiteLLM-internal parameters are passed through to the pod verbatim. LiteLLM-internal kwargs (things like `api_base`, `api_key`, `metadata`, `litellm_call_id`, etc.) are stripped before the request reaches the upstream pod.

### Sample curl

```bash
curl -sS https://litellm.adeoaiengine.ecouncil.ae/v1/audio/speech \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "inception-tts",
    "input": "Hello world, this is a text-to-speech test.",
    "voice": "alloy",
    "response_format": "mp3"
  }' \
  -o tts_output.mp3
```

Save the binary response to a file because the response body is raw audio, not JSON.

### Minimal curl (relying on defaults)

```bash
curl -sS https://litellm.adeoaiengine.ecouncil.ae/v1/audio/speech \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "inception-tts", "input": "Hello world."}' \
  -o tts_output.mp3
```

With no `voice` supplied, the gateway defaults it to `alloy` before forwarding.

### Response shape

The response is binary audio. The key aspects:

| Aspect | Value |
|---|---|
| HTTP status | `200` on success |
| Content-Type | `audio/mpeg` |
| Body | MP3 binary data |

The Inception TTS model is diffusion-based and non-deterministic, so two identical requests produce slightly different output sizes. This is expected model behavior, not gateway transformation. The gateway adds LiteLLM tracking headers (`x-litellm-call-id`, `x-litellm-model-id`, `x-litellm-response-duration-ms`, etc.) and strips pod-native headers (`server`, `x-audio-format`).

### Verify the output

```bash
file tts_output.mp3
# Expected: tts_output.mp3: Audio file with ID3 version ...
```

---

## Speech-to-text (STT)

### Request shape

STT uses a multipart form posted to `/v1/audio/transcriptions`. The gateway rebuilds the multipart form with a `model` field, a `file` field containing the audio, and any optional transcription parameters:

| Field | Required | Default | Notes |
|---|---|---|---|
| `model` | yes | | User-facing name `inception-stt` |
| `file` | yes | | The audio file to transcribe (multipart upload) |
| `language` | no | | Language code (e.g. `ar`, `en`) |
| `prompt` | no | | Optional context prompt for the transcription |
| `response_format` | no | | Desired response format |
| `temperature` | no | | Sampling temperature |
| `timestamp_granularities` | no | | Word or segment level timestamps |

As with TTS, LiteLLM-internal parameters are stripped before forwarding. The multipart form the pod receives contains only `model`, `file`, and your transcription parameters.

### Sample curl

```bash
curl -sS https://litellm.adeoaiengine.ecouncil.ae/v1/audio/transcriptions \
  -H "Authorization: Bearer <your-api-key>" \
  -F "model=inception-stt" \
  -F "file=@audio_sample.wav" \
  -F "language=ar"
```

The `-F` flags build the multipart form. Use `@` to attach the audio file from disk.

### Minimal curl (no optional params)

```bash
curl -sS https://litellm.adeoaiengine.ecouncil.ae/v1/audio/transcriptions \
  -H "Authorization: Bearer <your-api-key>" \
  -F "model=inception-stt" \
  -F "file=@audio_sample.wav"
```

### Response shape

The response is JSON. The pod returns `text`, `audio_duration`, and `word_timestamps`. The gateway normalizes the response to the OpenAI `TranscriptionResponse` schema by adding two fields:

| Field | Source | Example |
|---|---|---|
| `text` | Pod | `"شكرا."` |
| `audio_duration` | Pod | `1.0` |
| `word_timestamps` | Pod | `[]` |
| `task` | Gateway (added) | `"transcribe"` |
| `usage` | Gateway (added) | `null` |

Example response:

```json
{
  "text": "شكرا.",
  "usage": null,
  "task": "transcribe",
  "audio_duration": 1.0,
  "word_timestamps": []
}
```

The `task` and `usage` fields are added by `InceptionAudioTranscriptionConfig.transform_audio_transcription_response` so the response conforms to the OpenAI transcription schema. The transcription text and audio metadata are identical to what the pod returns directly.

---

## What the gateway transforms

The gateway applies three transformations on both endpoints. First, it maps the user-facing model name to the internal pod model via the database config lookup (`inception-tts` becomes `inception/inception-tts`, `inception-stt` becomes `inception/inception-stt`). Second, it strips LiteLLM-internal parameters (the `INCEPTION_INTERNAL_PARAMS` frozenset) so they never reach the upstream pod. Third, it manages headers: adding LiteLLM tracking headers and security headers, stripping pod-native headers like `server` and `x-audio-format`.

For STT specifically, the response is enriched with `task: "transcribe"` and `usage: null` to match the OpenAI `TranscriptionResponse` shape. The audio content and transcription text pass through unchanged.

No data loss or shape corruption occurs through the gateway. The request body you send is OpenAI-compatible, and the response you get back follows the OpenAI schema, with the pod's native fields preserved alongside the normalized ones.
