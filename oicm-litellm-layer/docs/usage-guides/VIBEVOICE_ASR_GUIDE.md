# VibeVoice ASR: Curl Guide

> **Scope**: How to call Microsoft VibeVoice automatic speech recognition (ASR) through the LiteLLM gateway, with sample curl commands and expected response shapes
>
> **Related docs**: [HAMSA_STT_TTS_GUIDE.md](HAMSA_STT_TTS_GUIDE.md) (native `/v1/audio/transcriptions` STT provider), [INCEPTION_TTS_STT_GUIDE.md](INCEPTION_TTS_STT_GUIDE.md) (similar OpenAI-compatible audio provider)

---

## Gateway endpoint

| Capability | Gateway URL |
|---|---|
| ASR (chat-style, audio-in) | `https://litellm.adeoaiengine.ecouncil.ae/v1/chat/completions` |

Authentication is the standard LiteLLM gateway key passed as a Bearer token. The VibeVoice pod itself does not require auth, but the gateway always requires it:

```
Authorization: Bearer <your-api-key>
```

The gateway maps the user-facing model name to the internal pod model. The discovery controller registers the model as `hosted_vllm/microsoft/VibeVoice-ASR`, so you send `microsoft/VibeVoice-ASR` as the model and the gateway resolves the provider, api_base, and routing.

> **Important**: VibeVoice-ASR is served as a **vLLM chat-completions** model, not through the native `/v1/audio/transcriptions` multipart endpoint. Audio is passed inline as a base64 `input_audio` content part inside a normal chat request. Calling `/v1/audio/transcriptions` returns `404 Not Found` from the upstream.

---

## Model overview

| Property | Value |
|---|---|
| Model ID | `microsoft/VibeVoice-ASR` |
| Underlying model | `hosted_vllm/microsoft/VibeVoice-ASR` (upstream: `VibeVoice-ASR-awq-int4`, served via vLLM `0.27.1`) |
| Type | Audio-to-text (ASR) with speaker diarization |
| Mode | `audio_transcription` |
| Input | Base64 WAV audio as an `input_audio` content part |
| Output | JSON array of segments `{Start, End, Speaker, Content}` inside `message.content` |

Confirm it is registered on the gateway:

```bash
curl -sk "https://litellm.adeoaiengine.ecouncil.ae/v1/models" \
  -H "Authorization: Bearer <your-api-key>"
```

---

## 1. Transcribe audio (single request)

### Endpoint

```
POST /v1/chat/completions
```

### Request shape

A standard OpenAI chat-completions request. The system prompt tells the model to emit JSON; the user message carries a text instruction plus the audio itself:

| Field | Required | Notes |
|---|---|---|
| `model` | yes | User-facing name `microsoft/VibeVoice-ASR` |
| `messages[0]` (system) | yes | Instructs the model to transcribe audio to JSON |
| `messages[1].content[0]` (text) | yes | Prompt that names the required keys |
| `messages[1].content[1]` (input_audio) | yes | The audio: `{"type":"input_audio","input_audio":{"data":"<base64>","format":"wav"}}` |
| `max_tokens` | no | Leave generous headroom (e.g. 300+) so the JSON array is not truncated |

The `input_audio.data` field is the base64-encoded WAV payload. Use the standard OpenAI content-type pattern for audio input.

### Sample curl

```bash
curl -sk https://litellm.adeoaiengine.ecouncil.ae/v1/chat/completions \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "microsoft/VibeVoice-ASR",
    "messages": [
      {
        "role": "system",
        "content": "You are a helpful assistant that transcribes audio input into text output in JSON format."
      },
      {
        "role": "user",
        "content": [
          {
            "type": "text",
            "text": "This is a 1.8 seconds audio, please transcribe it with these keys: Start time, End time, Speaker ID, Content"
          },
          {
            "type": "input_audio",
            "input_audio": {
              "data": "<base64-wav>",
              "format": "wav"
            }
          }
        ]
      }
    ],
    "max_tokens": 300
  }'
```

### Building the base64 payload

Encode your audio file before sending:

```bash
B64=$(python3 -c "import base64;print(base64.b64encode(open('audio.wav','rb').read()).decode())")
```

### Response shape

The response is an OpenAI `chat.completion`. The transcription is a JSON array of segments embedded as a string inside `message.content`, one object per detected speaker/utterance:

| Segment key | Type | Meaning |
|---|---|---|
| `Start` | float | Start time in seconds |
| `End` | float | End time in seconds |
| `Speaker` | int | Diarized speaker index (0-based) |
| `Content` | string | Transcribed text |

Example response:

```json
{
  "id": "chatcmpl-a883420fc9898ded",
  "created": 1788353170,
  "model": "microsoft/VibeVoice-ASR",
  "object": "chat.completion",
  "system_fingerprint": "vllm-0.27.1-f8eb07bf",
  "choices": [
    {
      "finish_reason": "stop",
      "index": 0,
      "message": {
        "content": "[{\"Start\":0,\"End\":1.8,\"Speaker\":0,\"Content\":\"[Music]\"}]\n",
        "role": "assistant",
        "provider_specific_fields": {
          "refusal": null,
          "reasoning": null
        }
      }
    }
  ],
  "usage": {
    "completion_tokens": 26,
    "prompt_tokens": 76,
    "total_tokens": 102
  }
}
```

The example above is from a live test: a 1.8-second WAV returned a single segment `{"Start":0,"End":1.8,"Speaker":0,"Content":"[Music]"}`. The `system_fingerprint` shows the upstream vLLM version (`vllm-0.27.1-f8eb07bf`).

---

## Response formatting tips

- **Give `max_tokens` headroom.** If `max_tokens` is too small, `finish_reason` becomes `"length"` and the JSON array is cut off mid-string. A few hundred tokens is safe for a short clip.
- **Parse the JSON string.** `message.content` is a JSON-encoded string; decode it (e.g. `json.loads`) before consuming the segments. It may be prefixed by a leading `assistant\n` in some responses.
- **Empty/non-speech audio** still yields a valid segment (e.g. `Content: "[Music]"`), so always handle the array.

---

## What the gateway transforms

The gateway maps the user-facing model name (`microsoft/VibeVoice-ASR`) to the internal pod deployment via the database config lookup, resolves the upstream `api_base` and the `hosted_vllm` provider, and forwards the OpenAI-compatible chat request. LiteLLM-internal params are stripped before the request reaches the upstream pod. No audio or transcription content is altered in transit.

---

## Quick reference

| Capability | Method | Endpoint | Model |
|---|---|---|---|
| ASR (audio in chat JSON) | `POST` | `/v1/chat/completions` | `microsoft/VibeVoice-ASR` |