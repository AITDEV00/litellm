# Inception TTS/STT: Gateway vs Pod-Direct Parity Check

> **Goal**: Verify that the request and response shapes from the LiteLLM gateway for inception TTS and STT match the shapes when calling the inception pods directly from within the cluster

>
> **Date**: 2026-08-03

---

## Test Setup

### Gateway path

External curl to the LiteLLM gateway:

```
https://litellm.adeoaiengine.ecouncil.ae/v1/audio/speech        (TTS)
https://litellm.adeoaiengine.ecouncil.ae/v1/audio/transcriptions (STT)
```

Auth: `Authorization: Bearer sk-1234`

### Pod-direct path

Python3 `urllib.request` from inside a litellm-proxy pod (`mlops/litellm-proxy-7c9bb48cbb-56pfg`) to the inception pods directly:

```
http://s-9aff17c0-988c-4cb1-98b9-e99faa6e9cdc.adeo.svc.cluster.local:8080/v1/audio/speech        (TTS)
http://s-e2e85fcc-c58a-4d41-91f7-1fffe6fc882b.adeo.svc.cluster.local:8080/v1/audio/transcriptions (STT)
```

No auth header (inception pods don't require authentication)

### Model config (from `/model/info` admin endpoint)

```
inception-tts:
  litellm_params:
    api_base: http://s-9aff17c0-988c-4cb1-98b9-e99faa6e9cdc.adeo.svc.cluster.local:8080/v1
    model: inception/inception-tts
    drop_params: true

inception-stt:
  litellm_params:
    api_base: http://s-e2e85fcc-c58a-4d41-91f7-1fffe6fc882b.adeo.svc.cluster.local:8080/v1
    model: inception/inception-stt
    drop_params: true
```

---

## TTS Parity Results

### Request shape

| Field | Gateway (user sends) | Pod-direct (we send) | LiteLLM transforms to |
|---|---|---|---|
| `model` | `inception-tts` | `inception/inception-tts` | `inception/inception-tts` (from `litellm_params.model`) |
| `input` | `"Hello world, this is a parity test."` | same | same (passed through) |
| `voice` | `"alloy"` | `"alloy"` | `"alloy"` (default if not provided) |
| Content-Type | `application/json` | `application/json` | `application/json` |

The gateway maps the user-facing model name `inception-tts` to the internal `litellm_params.model` value `inception/inception-tts` before forwarding to the pod. The request body shape is identical: OpenAI-compatible JSON with `model`, `input`, `voice` fields.

### Response shape

| Aspect | Gateway | Pod-direct |
|---|---|---|
| HTTP status | 200 | 200 |
| Content-Type | `audio/mpeg` | `audio/mpeg` |
| Body | MP3 binary | MP3 binary |
| Body size | 16080 bytes | 15960 bytes (first test: 15432) |

The body size difference is expected: the inception TTS model is diffusion-based and non-deterministic. Two pod-direct calls produced different sizes (15432 and 15960), confirming the variance is from the model, not from the gateway.

### Response headers

Gateway adds LiteLLM tracking headers not present in pod-direct response:

| Header | Gateway | Pod-direct |
|---|---|---|
| `server` | absent | `uvicorn` |
| `x-audio-format` | absent | `mp3` |
| `content-length` | absent (chunked) | `15960` |
| `x-litellm-call-id` | present | absent |
| `x-litellm-model-id` | present | absent |
| `x-litellm-model-api-base` | present | absent |
| `x-litellm-version` | present | absent |
| `x-litellm-key-spend` | present | absent |
| `x-litellm-response-duration-ms` | present | absent |
| `x-litellm-callback-duration-ms` | present | absent |
| Security headers | present (frame-options, CSP, etc.) | absent |

The gateway strips pod-native headers (`server`, `x-audio-format`) and adds LiteLLM tracking + security headers. The audio content itself is equivalent.

### TTS verdict

**Parity confirmed.** The request body shape is identical (OpenAI JSON). The response is the same media type (`audio/mpeg`) with equivalent binary content. Size differences are due to model non-determinism, not gateway transformation. The gateway adds tracking headers and strips pod-native headers, which is expected proxy behavior.

---

## STT Parity Results

### Request shape

| Field | Gateway (user sends) | Pod-direct (we send) | LiteLLM transforms to |
|---|---|---|---|
| `model` | `inception-stt` | `inception/inception-stt` | `inception/inception-stt` (from `litellm_params.model`) |
| `file` | multipart form file | multipart form file | multipart form file (via `process_audio_file`) |
| Content-Type | `multipart/form-data` | `multipart/form-data` | `multipart/form-data` |

The gateway maps the model name the same way as TTS. The multipart form structure is preserved: a `model` field and a `file` field with the audio content. LiteLLM's `transform_audio_transcription_request` uses `process_audio_file` to normalize the file handle and rebuilds the multipart form, but the resulting shape matches what the pod expects.

### Response shape

Pod-direct raw response:

```json
{"text":"شكرا.","audio_duration":1.0,"word_timestamps":[]}
```

Gateway response:

```json
{
  "text": "شكرا.",
  "usage": null,
  "task": "transcribe",
  "audio_duration": 1.0,
  "word_timestamps": []
}
```

The gateway adds two fields not present in the pod-direct response:

| Field | Gateway | Pod-direct | Source |
|---|---|---|---|
| `text` | `"شكرا."` | `"شكرا."` | Pod (identical) |
| `audio_duration` | `1.0` | `1.0` | Pod (identical) |
| `word_timestamps` | `[]` | `[]` | Pod (identical) |
| `usage` | `null` | absent | Added by `transform_audio_transcription_response` |
| `task` | `"transcribe"` | absent | Added by `transform_audio_transcription_response` |

The `usage` and `task` fields are added by `InceptionAudioTranscriptionConfig.transform_audio_transcription_response` (in `litellm/llms/inception/transcription/transformation.py`). This normalizes the response to the OpenAI `TranscriptionResponse` shape, which includes `usage` and `task` fields that the inception pod doesn't natively return.

### Response headers

Same pattern as TTS: gateway adds LiteLLM tracking headers, pod-direct returns uvicorn native headers.

### STT verdict

**Parity confirmed with expected enrichment.** The request body shape is identical (multipart form with model + file). The response content is identical (`text`, `audio_duration`, `word_timestamps` match exactly). The gateway adds `usage: null` and `task: "transcribe"` fields to normalize the response to the OpenAI `TranscriptionResponse` schema. This is intentional transformation, not a shape mismatch. The transcription text itself (`شكرا.`) is identical through both paths.

---

## Summary

| Endpoint | Request shape parity | Response content parity | Response shape difference |
|---|---|---|---|
| TTS (`/v1/audio/speech`) | Identical (OpenAI JSON) | Equivalent (MP3 binary, non-deterministic sizes) | Gateway adds tracking headers, strips pod headers |
| STT (`/v1/audio/transcriptions`) | Identical (multipart form) | Identical (same text, duration, timestamps) | Gateway adds `usage: null` and `task: "transcribe"` fields (OpenAI normalization) |

Both paths produce equivalent results. The gateway's transformations are:
1. Model name mapping: `inception-tts` → `inception/inception-tts` (DB config lookup)
2. Response normalization: STT response enriched with `usage` and `task` fields to match OpenAI schema
3. Header management: LiteLLM tracking headers added, pod-native headers stripped

No data loss or shape corruption occurs through the gateway path. The inception implementation correctly preserves the OpenAI-compatible request/response contract while adding the tracking metadata expected from a proxy.
