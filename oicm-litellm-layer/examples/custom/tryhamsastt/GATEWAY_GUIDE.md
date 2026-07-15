# LiteLLM Gateway Usage Guide

## Overview

The LiteLLM gateway provides a unified OpenAI-compatible API for accessing Hamsa TTS, Hamsa STT, LLM chat completions, and embedding models. All requests go through a single endpoint with a single API key.

**Gateway URL**: `https://litellm.adeoaiengine.ecouncil.ae`

**API Key**: `sk-omQbswRlepuTISV-1wgsDg`

All requests use the `Authorization: Bearer <api_key>` header unless otherwise noted.

## Available Models

| Model | Type | Description |
|---|---|---|
| `hamsa-tts` | Text-to-Speech | Arabic/English TTS with 110+ speakers |
| `hamsa-stt` | Speech-to-Text | Arabic STT (REST + WebSocket realtime) |
| `Qwen/Qwen3-Next-80B-A3B-Instruct` | LLM Chat | 80B parameter instruct model |
| `Qwen/Qwen3.5-122B-A10B-GPTQ-Int4` | LLM Chat | 122B reasoning model (outputs reasoning_content) |
| `Qwen3.6-35B-A3B-FP8` | LLM Chat | 35B reasoning model (outputs reasoning_content) |
| `MiniMaxAI/MiniMax-M3-MXFP8` | LLM Chat | MiniMax M3 reasoning model (outputs reasoning_content) |
| `Qwen/Qwen3-Embedding-4B` | Embeddings | 2560-dimensional embeddings |
| `Qwen/Qwen3-Embedding-0.6B` | Embeddings | 1024-dimensional embeddings |

### Reasoning Models

`Qwen/Qwen3.5-122B-A10B-GPTQ-Int4`, `Qwen3.6-35B-A3B-FP8`, and `MiniMaxAI/MiniMax-M3-MXFP8` are reasoning models. They produce output in two fields:

- `reasoning_content`: the chain-of-thought (not shown to end users)
- `content`: the final answer

These models need a higher `max_tokens` (e.g. 1000+) because reasoning tokens count toward the limit. With `max_tokens=50` the model may exhaust tokens on reasoning alone and return `content: null`.

---

## 1. Text-to-Speech (TTS)

### Endpoint

```
POST /v1/audio/speech
```

### Request Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `model` | string | Yes | - | Must be `hamsa-tts` |
| `input` | string | Yes | - | Text to synthesize |
| `voice` | string | No | `jasem` | Speaker name (case-insensitive) |
| `dialect` | string | No | speaker default | `msa`, `ksa`, or `eng` |
| `expressiveness` | float | No | `1.0` | 0.0-2.0, controls temperature and semantic tokens |
| `speed` | float | No | `1.0` | 0.5-2.0, speech speed multiplier |
| `response_format` | string | No | `wav` | `wav` (PCM 16kHz) or `mulaw` (8kHz mu-law) |

### Available Speakers

```
ASSY, AbdelQader, Ahmed, Akmal, Ali, Alia, Amanda, Amir, Amira, Amjad, Aml,
Arjun, Ayman, Barbara, Brian, Carla, Dalal, David, Dima, Edward, Eman, Eyad,
Fady, Fahd, Faiza, Fares, Fatma, Fouad, Gannat, Gassan, Ghazal, Hady, Hafsa,
Hamdan, Haneen, Hasan, Hatem, Hiba, Hind, Jaber, Jana, Jasem, John, Kamla,
Khadiga, Khadija, Lana, Layan, Layla, Lyali, Magda, Maha, Maher, Mai, Mais,
Majd, Majid, Mansour, Mariam, Marwa, Marwan, Mazen, Michael, Nabil, Nada,
Nadya, Nagib, Nermin, Noah, Noor, Noura, Nouran, Obida, Ola, Othman, Raghad,
Rami, Rania, Razan, Reem, Rema, Renat, Rihanna, Robert, Roger, Ruba, Safa,
Salam, Saleh, Salem, Salim, Salma, Salwa, Saly, Samer, Sami, Samir, Sandra,
Sarah, Sawsan, Sayed, Shaker, Somaya, Souad, Suzan, Talin, Tamer, Tasneem,
Wael, William, Yara, Yehya, Zeina
```

### Dialect Codes

- `msa` - Modern Standard Arabic (default for most speakers)
- `ksa` - Saudi Arabian dialect
- `eng` - English (auto-selected if text is >80% English)

### Expressiveness Mapping

| expressiveness | temperature | semantic tokens | Effect |
|---|---|---|---|
| 0.0 | 0.1 | no | Most deterministic, flat |
| 0.5 | 0.35 | no | Mild variation |
| 1.0 | 0.6 | no | Balanced (default) |
| 1.5 | 0.95 | yes | Expressive |
| 2.0 | 1.5 | yes | Most expressive, most random |

### Examples

**Arabic TTS (PCM WAV)**

```bash
curl -sk -X POST "https://litellm.adeoaiengine.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer sk-omQbswRlepuTISV-1wgsDg" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hamsa-tts",
    "input": "مرحبا بك في اختبار تحويل النص إلى كلام",
    "voice": "jasem",
    "dialect": "ksa",
    "expressiveness": 1.0,
    "response_format": "wav"
  }' \
  -o output.wav
```

**English TTS (mu-law)**

```bash
curl -sk -X POST "https://litellm.adeoaiengine.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer sk-omQbswRlepuTISV-1wgsDg" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hamsa-tts",
    "input": "Hello, welcome to the text to speech test.",
    "voice": "amanda",
    "response_format": "mulaw"
  }' \
  -o output_mulaw.wav
```

### Response

Binary audio data with `Content-Type: audio/wav`. The audio is 16kHz mono 16-bit PCM (when `response_format=wav`) or 8kHz mono mu-law (when `response_format=mulaw`).

---

## 2. Voice Management

### Endpoint

```
POST /v1/audio/voices
```

Used to register a custom cloned voice or load a pre-extracted voice profile.

### Request Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `model` | string | Yes | Must be `hamsa-tts` |
| `action` | string | No | `register` (default) or `load` |
| `speaker_id` | string | Yes | Name for the new voice |
| `global_token_ids` | array | Yes (for `load`) | Global token arrays from voice cloning |
| `semantic_token_ids` | array | Yes (for `load`) | Semantic token arrays from voice cloning |
| `dialect` | string | No | `msa`, `ksa`, or `eng` (default: `msa`) |
| `prompt_text` | string | No | Reference transcript |

### Example: Load a Cloned Voice

```bash
curl -sk -X POST "https://litellm.adeoaiengine.ecouncil.ae/v1/audio/voices" \
  -H "Authorization: Bearer sk-omQbswRlepuTISV-1wgsDg" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hamsa-tts",
    "action": "load",
    "speaker_id": "my_custom_voice",
    "global_token_ids": [[1,2,3],[4,5,6]],
    "semantic_token_ids": [[10,20],[30,40]],
    "dialect": "ksa",
    "prompt_text": "Reference transcript"
  }'
```

### Response

```json
{"voice_id": "", "status": "registered"}
```

After registering, the new voice is immediately usable in `/v1/audio/speech` by passing `"voice": "my_custom_voice"`.

**Note**: Custom voices exist only in the model's in-memory state. If the TTS pod restarts, custom voices are lost and must be re-registered.

---

## 3. Speech-to-Text (STT) - REST API

### Endpoint

```
POST /v1/audio/transcriptions
```

### Request Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `model` | string | Yes | Must be `hamsa-stt` |
| `file` | file | Yes | Audio file (PCM 16kHz 16-bit WAV recommended) |
| `language` | string | No | Language code (default: `auto`) |

### Example

```bash
curl -sk -X POST "https://litellm.adeoaiengine.ecouncil.ae/v1/audio/transcriptions" \
  -H "Authorization: Bearer sk-omQbswRlepuTISV-1wgsDg" \
  -F "model=hamsa-stt" \
  -F "file=@audio.wav" \
  -F "language=auto"
```

### Response

```json
{
  "text": "مرحبا بك في اختبار تحويل النص إلى كلام",
  "gender": "Male",
  "eos": {"prediction": 1},
  "processing_time": 0.58,
  "duration": 2.1,
  "speaker_embeddings": [...],
  "wake_word_match": null
}
```

### Audio Format Requirements

The STT service expects **PCM 16kHz 16-bit mono WAV** files. Mu-law encoded WAV files will fail with "buffer size must be a multiple of element size". If you have mu-law audio, convert it first:

```bash
ffmpeg -i input_mulaw.wav -ar 16000 -ac 1 -sample_fmt s16 output_pcm.wav
```

---

## 4. Speech-to-Text (STT) - WebSocket Realtime

### Endpoint

```
wss://litellm.adeoaiengine.ecouncil.ae/v1/realtime?model=hamsa-stt
```

The WebSocket connection enables real-time streaming transcription from a microphone or audio stream.

### Authentication

Browsers cannot set custom headers on WebSocket connections, so the API key is passed via the `sec-websocket-protocol` subprotocol:

```javascript
const ws = new WebSocket(
  "wss://litellm.adeoaiengine.ecouncil.ae/v1/realtime?model=hamsa-stt",
  ["openai-insecure-api-key.sk-omQbswRlepuTISV-1wgsDg"]
);
```

### Protocol

1. **Client sends handshake** (JSON) after connection opens:

```json
{
  "type": "handshake",
  "api_key": "gAAAAABo-1oxslqx1hGc8nGn_7iWiD0jwAGE7tDk3MgA-t_9gM05qFZIP1tTiBgJpDkTaTrf7OHe9RLj2AjspUYuKxAqVnPjIJ6AD6q-0E8paCMBreZ8pGc=",
  "authorization": "Bearer sk-omQbswRlepuTISV-1wgsDg",
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

2. **Server responds** with handshake acknowledgment:

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

| Option | Type | Default | Description |
|---|---|---|---|
| `silence_timeout` | float | 30 | Seconds of silence before auto-disconnect |
| `sample_rate` | int | 16000 | Audio sample rate (must be 16000) |
| `min_silence_duration_ms` | int | 300 | Minimum silence to trigger end-of-utterance |
| `min_speech_ms` | int | 600 | Minimum speech duration before processing |
| `client_logging` | bool | true | Enable server-side logging |
| `vad_threshold` | float | 0.6 | Voice activity detection sensitivity (0.1-0.99) |
| `eos_enabled` | bool | true | Enable end-of-speech detection |
| `eos_threshold` | float | 0.6 | End-of-speech detection threshold (0.1-0.99) |
| `audio_type` | string | "PCM" | Audio encoding type |
| `noise_cancellation` | bool | false | Enable noise cancellation |

### Interactive Test Page

An interactive WebSocket test page is available at `test-ws.html` in this directory. To use it:

```bash
cd oicm-litellm-layer/examples/custom/tryhamsastt
python3 -m http.server 8765
```

Then open `http://localhost:8765/test-ws.html` in a browser. The page is pre-configured with the correct gateway URL, API keys, and default options. Click "Connect & Start Recording" to begin streaming microphone audio for real-time transcription.

---

## 5. LLM Chat Completions

### Endpoint

```
POST /v1/chat/completions
```

Standard OpenAI-compatible chat completions API.

### Example: Standard Model

```bash
curl -sk -X POST "https://litellm.adeoaiengine.ecouncil.ae/v1/chat/completions" \
  -H "Authorization: Bearer sk-omQbswRlepuTISV-1wgsDg" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-Next-80B-A3B-Instruct",
    "messages": [
      {"role": "user", "content": "Say hello in Arabic and English."}
    ],
    "max_tokens": 100
  }'
```

### Response

```json
{
  "id": "chatcmpl-...",
  "model": "Qwen/Qwen3-Next-80B-A3B-Instruct",
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": "Hello in Arabic: مرحبًا\nHello in English: Hello"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "completion_tokens": 14,
    "prompt_tokens": 19,
    "total_tokens": 33
  }
}
```

### Example: Reasoning Model

Reasoning models (`Qwen3.5-122B`, `Qwen3.6-35B`, `MiniMax-M3`) produce a `reasoning_content` field in addition to `content`. Use a higher `max_tokens`:

```bash
curl -sk -X POST "https://litellm.adeoaiengine.ecouncil.ae/v1/chat/completions" \
  -H "Authorization: Bearer sk-omQbswRlepuTISV-1wgsDg" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "MiniMaxAI/MiniMax-M3-MXFP8",
    "messages": [
      {"role": "user", "content": "What is 2+2?"}
    ],
    "max_tokens": 500
  }'
```

### Response

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "reasoning_content": "The user is asking a simple math question...",
        "content": "Four"
      },
      "finish_reason": "stop"
    }
  ]
}
```

If `max_tokens` is too low, the model exhausts tokens on reasoning and returns `"content": null` with `"finish_reason": "length"`.

### Available LLM Models

| Model | Type | Notes |
|---|---|---|
| `Qwen/Qwen3-Next-80B-A3B-Instruct` | Standard | Best for general chat, fast response |
| `Qwen/Qwen3.5-122B-A10B-GPTQ-Int4` | Reasoning | Large model, needs high max_tokens |
| `Qwen3.6-35B-A3B-FP8` | Reasoning | Mid-size, needs high max_tokens |
| `MiniMaxAI/MiniMax-M3-MXFP8` | Reasoning | MiniMax M3, needs high max_tokens |

---

## 6. Embeddings

### Endpoint

```
POST /v1/embeddings
```

### Request Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `model` | string | Yes | `Qwen/Qwen3-Embedding-4B` or `Qwen/Qwen3-Embedding-0.6B` |
| `input` | string or array | Yes | Text or list of texts to embed |

### Example

```bash
curl -sk -X POST "https://litellm.adeoaiengine.ecouncil.ae/v1/embeddings" \
  -H "Authorization: Bearer sk-omQbswRlepuTISV-1wgsDg" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-Embedding-4B",
    "input": "Hello world"
  }'
```

### Response

```json
{
  "data": [
    {
      "embedding": [0.000169, -0.028553, -0.000493, 0.041785, ...],
      "index": 0
    }
  ],
  "model": "Qwen/Qwen3-Embedding-4B",
  "usage": {
    "prompt_tokens": 3,
    "total_tokens": 3
  }
}
```

### Embedding Dimensions

| Model | Dimensions |
|---|---|
| `Qwen/Qwen3-Embedding-4B` | 2560 |
| `Qwen/Qwen3-Embedding-0.6B` | 1024 |

### Batch Embeddings

```bash
curl -sk -X POST "https://litellm.adeoaiengine.ecouncil.ae/v1/embeddings" \
  -H "Authorization: Bearer sk-omQbswRlepuTISV-1wgsDg" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen3-Embedding-0.6B",
    "input": ["First text", "Second text", "Third text"]
  }'
```

---

## 7. Model Listing

### Endpoint

```
GET /v1/models
```

### Example

```bash
curl -sk "https://litellm.adeoaiengine.ecouncil.ae/v1/models" \
  -H "Authorization: Bearer sk-omQbswRlepuTISV-1wgsDg" | python3 -m json.tool
```

Returns the list of models accessible with this API key.

---

## Quick Reference

All endpoints use the same base URL and API key:

```
Base URL:  https://litellm.adeoaiengine.ecouncil.ae
API Key:   sk-omQbswRlepuTISV-1wgsDg
Header:    Authorization: Bearer sk-omQbswRlepuTISV-1wgsDg
```

| Endpoint | Method | Path | Model |
|---|---|---|---|
| Text-to-Speech | POST | `/v1/audio/speech` | `hamsa-tts` |
| Voice Management | POST | `/v1/audio/voices` | `hamsa-tts` |
| Speech-to-Text (REST) | POST | `/v1/audio/transcriptions` | `hamsa-stt` |
| Speech-to-Text (WS) | WS | `/v1/realtime?model=hamsa-stt` | `hamsa-stt` |
| Chat Completions | POST | `/v1/chat/completions` | Qwen / MiniMax models |
| Embeddings | POST | `/v1/embeddings` | Qwen embedding models |
| Model List | GET | `/v1/models` | - |
