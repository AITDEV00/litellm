# Hamsa STT & TTS Usage Guide

This guide covers the Hamsa Speech-to-Text (STT) and Text-to-Speech (TTS) endpoints exposed through the LiteLLM gateway. All requests use a single base URL and a single API key.

**Gateway Base URL**: `https://litellm.ecouncil.ae/v1`

**API Key**: replace `<YOUR_API_KEY>` everywhere below with your LiteLLM gateway API key (e.g. `sk-...`). All requests use the `Authorization: Bearer <YOUR_API_KEY>` header unless noted otherwise.

The gateway is properly SSL-verified, so the curl examples below use standard HTTPS verification (no `-k`/`--insecure` flag).

> **Interactive test page**: a browser-based WebSocket client for realtime Hamsa STT is available at `examples/custom/tryhamsastt/hamsa-stt-realtime-test-ws.html`. See [Realtime STT via WebSocket](#4-realtime-stt-websocket).

---

## Available Hamsa Models

| Model | Type | Description |
|---|---|---|
| `hamsa-tts` | Text-to-Speech | Arabic/English TTS with 110+ speakers |
| `hamsa-stt` | Speech-to-Text | Arabic STT (REST + WebSocket realtime) |

Confirm they are registered on the gateway:

```bash
curl -s "https://litellm.ecouncil.ae/v1/models" \
  -H "Authorization: Bearer <YOUR_API_KEY>"
```

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
Salam, Saleh, Salem, Salim, Salwa, Saly, Samer, Sami, Samir, Sandra,
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

### Example — Arabic TTS (PCM WAV)

```bash
curl -s -X POST "https://litellm.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
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

### Example — English TTS (mu-law)

```bash
curl -s -X POST "https://litellm.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
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

Binary audio data with `Content-Type: audio/wav`. When `response_format=wav` the audio is 16kHz mono 16-bit PCM; when `response_format=mulaw` it is 8kHz mono mu-law.

---

## 2. Voice Management

### Endpoint

```
POST /v1/audio/voices
```

Voice cloning is a two-step process:

- **Step 1 - Extract tokens** (`action: "register"`): pass an audio URL and transcript. The Hamsa backend downloads the audio and runs it through the BiCodecTokenizer to extract `global_token_ids` and `semantic_token_ids`.
- **Step 2 - Load tokens** (`action: "load"`): pass the extracted tokens with a `speaker_id` to register the voice in the model's in-memory speaker dictionary. After loading, the voice is immediately usable in `/v1/audio/speech`.

### Request Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `model` | string | Yes | Must be `hamsa-tts` |
| `action` | string | No | `register` (default) or `load` |
| `speaker_id` | string | Yes (for `load`) | Name for the new voice |
| `audio_url` | string | Yes (for `register`) | URL to the reference audio file |
| `prompt_text` | string | Yes (for `register`) | Transcript of the reference audio |
| `global_token_ids` | array | Yes (for `load`) | Global token arrays from step 1 |
| `semantic_token_ids` | array | Yes (for `load`) | Semantic token arrays from step 1 |
| `dialect` | string | No | `msa`, `ksa`, or `eng` (default: `msa`) |

### Known Limitation: Step 1 (register) is Currently Broken

The Hamsa TTS pod runs with a **read-only root filesystem**. Step 1 (`action: "register"`) calls `download_file()` internally, which tries to create a `ref_audios/` directory in `/app` (the working directory) to save the downloaded audio. This fails with:

```
OSError: [Errno 30] Read-only file system: 'ref_audios'
```

Verified through the gateway:

```bash
curl -s -X POST "https://litellm.ecouncil.ae/v1/audio/voices" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hamsa-tts",
    "action": "register",
    "audio_url": "https://example.com/reference.wav",
    "prompt_text": "This is the transcript of the reference audio."
  }'
# Returns: HTTP 500 Internal Server Error
```

The pod has writable directories (`/app/output`, `/app/download`, `/tmp`), but `download_file()` uses a hardcoded relative path (`ref_audios`). This is a bug in the Hamsa TTS service, not in the LiteLLM gateway.

**Workaround**: extract voice tokens manually by exec'ing into the pod, then use `action: "load"` (step 2) through the gateway to register the voice.

### Example — Load a Cloned Voice (Step 2)

```bash
curl -s -X POST "https://litellm.ecouncil.ae/v1/audio/voices" \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hamsa-tts",
    "action": "load",
    "speaker_id": "my_custom_voice",
    "global_token_ids": [[2835, 1337, 574, 3897, 305, 1557, 1106, 58, 3449, 3190]],
    "semantic_token_ids": [[4934, 4577, 5060, 5288, 7108, 3210, 4001, 1234, 5678, 9012]],
    "dialect": "ksa",
    "prompt_text": "Reference transcript"
  }'
```

Response:

```json
{"voice_id": "", "status": "registered"}
```

After loading, the voice is usable in `/v1/audio/speech` by passing `"voice": "my_custom_voice"`.

> **Note**: custom voices exist only in the model's in-memory state. If the TTS pod restarts, custom voices are lost and must be re-loaded.

---

## 3. Speech-to-Text (STT) — REST API

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

## 4. Realtime STT (WebSocket)

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

A ready-to-use browser client is provided at `examples/custom/tryhamsastt/hamsa-stt-realtime-test-ws.html`. It is pre-configured with the correct gateway URL. To use it:

```bash
cd oicm-litellm-layer/examples/custom/tryhamsastt
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

---

## Quick Reference

| Capability | Method | Endpoint |
|---|---|---|
| TTS | `POST` | `/v1/audio/speech` |
| Voice clone (load) | `POST` | `/v1/audio/voices` |
| STT (REST) | `POST` | `/v1/audio/transcriptions` |
| STT (realtime) | `WS` | `/v1/realtime?model=hamsa-stt` |