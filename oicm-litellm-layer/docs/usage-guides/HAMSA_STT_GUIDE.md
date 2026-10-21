# Hamsa STT Usage Guide

This guide covers the Hamsa Speech-to-Text (STT) endpoints exposed through the
LiteLLM gateway: batch transcription over REST and streaming transcription over
WebSocket. Both use the same base URL and the same API key.

**Gateway Base URL**: `https://litellm.ecouncil.ae/v1`

**API Key**: replace `<YOUR_API_KEY>` everywhere below with your LiteLLM gateway API key (e.g. `sk-...`). All requests use the `Authorization: Bearer <YOUR_API_KEY>` header unless noted otherwise.

The gateway is properly SSL-verified, so the curl examples below use standard HTTPS verification (no `-k`/`--insecure` flag).

> **Scope**: STT only. For Hamsa text-to-speech, see
> [HAMSA-TTS-NEW-V1-GUIDE.md](HAMSA-TTS-NEW-V1-GUIDE.md), which documents the
> active `hamsa-tts-new` model. The original `hamsa-tts` deployment is retired:
> its backend Service was removed and its model row was deleted from the gateway,
> so requesting it fails with HTTP 400 "Invalid model name". This guide no longer
> documents it.

> **Interactive test page**: a browser-based WebSocket client for realtime Hamsa STT is available at `examples/usage/hamsa-stt/hamsa-stt-realtime-test-ws.html`. See [Realtime STT via WebSocket](#2-realtime-stt-websocket).

---

## Available Hamsa STT Model

| Model | Type | Status | Description |
| --- | --- | --- | --- |
| `hamsa-stt` | Speech-to-Text | Active | Arabic STT (REST + WebSocket realtime) |

For Hamsa text-to-speech (`hamsa-tts-new`), see
[HAMSA-TTS-NEW-V1-GUIDE.md](HAMSA-TTS-NEW-V1-GUIDE.md).

Confirm it is registered on the gateway:

```bash
curl -s "https://litellm.ecouncil.ae/v1/models" \
  -H "Authorization: Bearer <YOUR_API_KEY>"
```

---

## 1. Speech-to-Text (STT) — REST API

### Endpoint

```
POST /v1/audio/transcriptions
```

### Request Parameters

| Parameter    | Type   | Required | Description                                   |
| ------------ | ------ | -------- | --------------------------------------------- |
| `model`    | string | Yes      | Must be`hamsa-stt`                          |
| `file`     | file   | Yes      | Audio file (PCM 16kHz 16-bit WAV recommended) |
| `language` | string | No       | Language code (default:`auto`)              |

### Example

```bash
curl -s -X POST "https://litellm.ecouncil.ae/v1/audio/transcriptions" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -F "model=hamsa-stt" \
  -F "file=@audio.wav" \
  -F "language=auto"
```

### Response

```json
{
  "text": "مرحبا بك في اختبار تحويل النص إلى كلام",
  "usage": null,
  "task": "transcribe",
  "gender": "Male",
  "eos": {"prediction": 1},
  "processing_time": 0.58,
  "duration": 2.1,
  "speaker_embeddings": null,
  "wake_word_match": null,
  "similarity_score": null
}
```

### Audio Format Requirements

The STT service expects **PCM 16kHz 16-bit mono WAV** files. Mu-law encoded WAV files will fail with "buffer size must be a multiple of element size". If you have mu-law audio, convert it first:

```bash
ffmpeg -i input_mulaw.wav -ar 16000 -ac 1 -sample_fmt s16 output_pcm.wav
```

---

## 2. Realtime STT (WebSocket)

### Endpoint

```
wss://litellm.ecouncil.ae/v1/realtime?model=hamsa-stt
```

The WebSocket connection enables real-time streaming transcription from a microphone or audio stream.

### Authentication

Browsers cannot set custom headers on WebSocket connections, so the API key is passed via the `sec-websocket-protocol` subprotocol:

```javascript
const ws = new WebSocket(
  "wss://litellm.ecouncil.ae/v1/realtime?model=hamsa-stt",
  ["openai-insecure-api-key.<YOUR_API_KEY>"]
);
```

### Interactive Test Page

A ready-to-use browser client is provided at `examples/usage/hamsa-stt/hamsa-stt-realtime-test-ws.html`. It is pre-configured with the correct gateway URL. To use it:

```bash
cd oicm-litellm-layer/examples/usage/hamsa-stt
python3 -m http.server 8765
```

Then open `http://localhost:8765/hamsa-stt-realtime-test-ws.html` in a browser. Enter your gateway API key, then click **Connect & Start Recording** to stream microphone audio for realtime transcription.

### Protocol

1. **Client sends handshake** (JSON) after the connection opens:

```json
{
  "type": "handshake",
  "api_key": "<HAMSA_STT_ENCRYPTED_KEY>",
  "authorization": "Bearer <YOUR_API_KEY>",
  "options": {
    "silence_timeout": 30,
    "sample_rate": 16000,
    "min_silence_duration_ms": 300,
    "min_speech_ms": 600,
    "client_logging": true,
    "vad_threshold": 0.6,
    "eos_enabled": true,
    "eos_threshold": 0.6,
    "audio_type": "PCM",
    "noise_cancellation": false
  }
}
```

The `api_key` field is the Hamsa STT encrypted key (Fernet token). The `authorization` field is the LiteLLM gateway API key.

2. **Server responds** with a handshake acknowledgment:

```json
{"type": "handshake_ack"}
```

3. **Client streams audio** as binary PCM data (16kHz, 16-bit, mono, little-endian). Send `Int16Array` buffers from the microphone.
4. **Server sends transcription results**:

```json
{
  "type": "transcription",
  "duration_ms": 2500,
  "data": {
    "transcription": "مرحبا بك في اختبار تحويل النص إلى كلام",
    "gender": "Male",
    "eos": {"prediction": 1}
  }
}
```

5. **Server may send log messages**:

```json
{"type": "log", "data": "VAD detected speech start"}
```

6. **Server may send errors**:

```json
{"type": "error", "error": "Invalid audio format"}
```

### WebSocket Options

| Option                      | Type   | Default | Description                                     |
| --------------------------- | ------ | ------- | ----------------------------------------------- |
| `silence_timeout`         | float  | 30      | Seconds of silence before auto-disconnect       |
| `sample_rate`             | int    | 16000   | Audio sample rate (must be 16000)               |
| `min_silence_duration_ms` | int    | 300     | Minimum silence to trigger end-of-utterance     |
| `min_speech_ms`           | int    | 600     | Minimum speech duration before processing       |
| `client_logging`          | bool   | true    | Enable server-side logging                      |
| `vad_threshold`           | float  | 0.6     | Voice activity detection sensitivity (0.1-0.99) |
| `eos_enabled`             | bool   | true    | Enable end-of-speech detection                  |
| `eos_threshold`           | float  | 0.6     | End-of-speech detection threshold (0.1-0.99)    |
| `audio_type`              | string | "PCM"   | Audio encoding type                             |
| `noise_cancellation`      | bool   | false   | Enable noise cancellation                       |

---

## Quick Reference

| Capability      | Method | Endpoint                       |
| --------------- | ------ | ------------------------------ |
| STT (REST)      | `POST` | `/v1/audio/transcriptions`     |
| STT (realtime)  | `WS`   | `/v1/realtime?model=hamsa-stt` |
