# Hamsa STT Research: Source Code Crawl, Protocol Analysis, and OpenAI Compatibility Assessment

## 1. K8s Access Commands

SSH tunnel to the K8s API server (needed before any kubectl command):

```bash
sshpass -p 'Password123' ssh -fN \
  -o StrictHostKeyChecking=no \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -L 6443:10.34.104.10:6443 \
  adeo@10.34.104.99
```

All kubectl commands use:

```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl ...
```

## 2. Hamsa Pod Discovery

Find the Hamsa STT pod:

```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl get pod -n adeo
```

Key identifiers:

```text
Pod:      j-9c57bce9-0583-4bf7-9443-08825220a231-8458b5c7b7-x8wbr
Service:  s-9c57bce9-0583-4bf7-9443-08825220a231 (ClusterIP, namespace: adeo)
Endpoint: 10.42.8.132:8080
URL:      ws://s-9c57bce9-0583-4bf7-9443-08825220a231.adeo.svc.cluster.local:8080
```

## 3. Source Code Crawl Commands

### 3.1 List the Hamsa app directory

```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl exec -n adeo \
  j-9c57bce9-0583-4bf7-9443-08825220a231-8458b5c7b7-x8wbr -- \
  ls -la /app/app/
```

### 3.2 Read main.py (FastAPI app + routes)

```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl exec -n adeo \
  j-9c57bce9-0583-4bf7-9443-08825220a231-8458b5c7b7-x8wbr -- \
  cat /app/app/main.py
```

### 3.3 Read ws_handler.py (WebSocket protocol + VAD)

```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl exec -n adeo \
  j-9c57bce9-0583-4bf7-9443-08825220a231-8458b5c7b7-x8wbr -- \
  cat /app/app/ws_handler.py
```

### 3.4 Read auth.py (Fernet encryption)

```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl exec -n adeo \
  j-9c57bce9-0583-4bf7-9443-08825220a231-8458b5c7b7-x8wbr -- \
  cat /app/app/auth.py
```

### 3.5 Read .env (encryption keys + config)

```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl exec -n adeo \
  j-9c57bce9-0583-4bf7-9443-08825220a231-8458b5c7b7-x8wbr -- \
  cat /app/.env
```

## 4. Endpoint Testing Commands

### 4.1 REST /transcribe (from inside Hamsa pod)

```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl exec -n adeo \
  j-9c57bce9-0583-4bf7-9443-08825220a231-8458b5c7b7-x8wbr -- \
  python3 -c "
import urllib.request, json, base64

with open('/app/test-adeo-30s.wav', 'rb') as f:
    audio_b64 = base64.b64encode(f.read()).decode()

payload = json.dumps({
    'audio': audio_b64,
    'lang': 'auto',
    'eos_enabled': False,
    'threshold': 0.3
}).encode()

req = urllib.request.Request(
    'http://localhost:8080/transcribe',
    data=payload,
    headers={
        'Content-Type': 'application/json',
        'x-api-key': 'gAAAAABo-1oxslqx1hGc8nGn_7iWiD0jwAGE7tDk3MgA-t_9gM05qFZIP1tTiBgJpDkTaTrf7OHe9RLj2AjspUYuKxAqVnPjIJ6AD6q-0E8paCMBreZ8pGc='
    }
)
resp = urllib.request.urlopen(req)
print(json.loads(resp.read()))
"
```

Result:

```json
{
  "text": "السلام عليكم ورحمة الله وبركاته، اليوم الاربعاء الثامن عشر من ديسمبر 2025...",
  "gender": "Male",
  "eos": null,
  "processing_time": 1.088,
  "duration": 29.83
}
```

### 4.2 WebSocket /ws (from LiteLLM pod, with real-time pacing)

This was the critical test. Sending all chunks at once produces zero transcriptions because the VAD (Silero) expects streaming audio with natural speech/silence patterns. Adding a 100ms delay between chunks (simulating real microphone pacing) triggers the VAD correctly.

```bash
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl exec -n mlops \
  litellm-proxy-57b4c9985d-6c8bx -- python3 -c "
import asyncio, websockets, json, wave, numpy as np

async def test():
    target = 'ws://s-9c57bce9-0583-4bf7-9443-08825220a231.adeo.svc.cluster.local:8080/ws'
    upstream_api_key = 'gAAAAABo-1oxslqx1hGc8nGn_7iWiD0jwAGE7tDk3MgA-t_9gM05qFZIP1tTiBgJpDkTaTrf7OHe9RLj2AjspUYuKxAqVnPjIJ6AD6q-0E8paCMBreZ8pGc='

    async with websockets.connect(target) as ws:
        handshake = {
            'type': 'handshake',
            'api_key': upstream_api_key,
            'options': {
                'silence_timeout': 30,
                'sample_rate': 16000,
                'min_silence_duration_ms': 300,
                'min_speech_ms': 600,
                'client_logging': True,
                'vad_threshold': 0.3,
                'eos_enabled': True,
                'eos_threshold': 0.3,
                'audio_type': 'PCM',
                'noise_cancellation': False
            }
        }
        await ws.send(json.dumps(handshake))
        ack = await asyncio.wait_for(ws.recv(), timeout=10)
        print(f'Handshake ack received')

        with open('/tmp/test-adeo-30s.wav', 'rb') as f:
            with wave.open(f) as wav:
                sample_rate = wav.getframerate()
                n_frames = wav.getnframes()
                n_channels = wav.getnchannels()
                audio_data = wav.readframes(n_frames)

        # Resample 48kHz -> 16kHz using numpy interpolation
        audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
        if n_channels > 1:
            audio_np = audio_np.reshape(-1, n_channels).mean(axis=1)
        if sample_rate != 16000:
            num_samples = int(len(audio_np) * 16000 / sample_rate)
            ratio = num_samples / len(audio_np)
            indices = np.arange(num_samples) / ratio
            audio_np = np.interp(indices, np.arange(len(audio_np)), audio_np)
        audio_int16 = audio_np.astype(np.int16)
        raw_pcm = audio_int16.tobytes()

        chunk_size = 3200  # 100ms of 16kHz 16-bit mono audio
        chunks = [raw_pcm[i:i+chunk_size] for i in range(0, len(raw_pcm), chunk_size)]

        async def send_audio():
            for chunk in chunks:
                await ws.send(chunk)
                await asyncio.sleep(0.1)  # CRITICAL: real-time pacing
            print(f'Sent {len(chunks)} chunks')

        async def receive_messages():
            transcriptions = []
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=20)
                    data = json.loads(msg)
                    if data['type'] == 'transcription':
                        transcriptions.append(data)
                        t = data['data'].get('transcription', '')
                        print(f'Transcription: {t!r}')
                    elif data['type'] == 'log':
                        print(f'Log: {data[\"data\"]}')
                    else:
                        print(f'Unknown: {data[\"type\"]}')
            except asyncio.TimeoutError:
                print('No more messages')
            print(f'Total transcriptions: {len(transcriptions)}')

        await asyncio.gather(send_audio(), receive_messages())

asyncio.run(test())
"
```

Result:

```text
Handshake ack received
WAV: 48000Hz, 1ch, 2864110 bytes
Resampled to 16kHz: 477351 samples
Raw PCM: 954702 bytes
Log: Speech detected. Running HamsaASR transcription...
Transcription: 'السلام عليكم ورحمة الله وبركاته، اليوم الأربعاء ال 18 من ديسمبر 2025'
Log: Speech detected. Running HamsaASR transcription...
Transcription: 'اليوم عندنا meetings updates كتيرة مع فيصل عبد الرحفار الهاوي على'
Log: Speech detected. Running HamsaASR transcription...
Transcription: 'مذكرات الذكاء الاصطناعي، وتقارير الذكاء الاصطناعي'
Sent 299 chunks
No more messages
Total transcriptions: 3
```

### 4.3 File copy commands (test audio)

Copy test audio from Hamsa pod to local, then to LiteLLM pod:

```bash
# From Hamsa pod to local
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl cp \
  adeo/j-9c57bce9-0583-4bf7-9443-08825220a231-8458b5c7b7-x8wbr:/app/test-adeo-30s.wav \
  /tmp/test-adeo-30s.wav

# From local to LiteLLM pod
KUBECONFIG=$HOME/.kube/oicm-alain.conf kubectl cp \
  /tmp/test-adeo-30s.wav \
  mlops/litellm-proxy-57b4c9985d-6c8bx:/tmp/test-adeo-30s.wav
```

## 5. Self-Hosted Hamsa Protocol (from source code)

### 5.1 REST API: POST /transcribe

**Auth**: `x-api-key` header with Fernet-encrypted key value.

**Request** (JSON body):

```json
{
  "audio": "base64-string-or-list-of-floats",
  "prompt": "optional context prompt",
  "lang": "auto",
  "eos_enabled": false,
  "eos_threshold": 0.3,
  "gender_detection": false,
  "speaker_identification": false,
  "wake_word": false,
  "threshold": 0.3
}
```

**Response**:

```json
{
  "text": "transcribed text",
  "gender": "Male",
  "eos": null,
  "processing_time": 1.088,
  "duration": 29.83,
  "speaker_embeddings": null,
  "wake_word_match": null,
  "similarity_score": null
}
```

### 5.2 WebSocket API: WS /ws

**Auth**: `api_key` field inside the handshake JSON message (not HTTP headers). Value is the Fernet-encrypted key.

**Protocol flow**:

```text
Client -> Server: handshake message (JSON, text)
Server -> Client: handshake_ack message (JSON, text)
Client -> Server: binary PCM audio chunks (or {"type":"media","payload":base64} text messages)
Server -> Client: transcription messages (JSON, text) - triggered by VAD
Server -> Client: log messages (JSON, text)
Server -> Client: error messages (JSON, text)
```

**Handshake message**:

```json
{
  "type": "handshake",
  "api_key": "gAAAAAB...Fernet-encrypted...",
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

**Transcription message** (server -> client):

```json
{
  "type": "transcription",
  "data": {
    "transcription": "السلام عليكم",
    "gender": "Male",
    "eos": { "prediction": 1 },
    "speaker_info": null
  },
  "duration_ms": 3200
}
```

**Log message** (server -> client):

```json
{
  "type": "log",
  "data": "Speech detected. Running HamsaASR transcription...",
  "duration_ms": 100
}
```

**Audio format**: Raw PCM 16-bit signed, 16kHz, mono. Chunk size 3200 bytes (100ms). Must be sent with real-time pacing (100ms between chunks) for VAD to trigger correctly. Dumping all chunks at once produces zero transcriptions.

**VAD**: Silero VAD (JIT-loaded). Configurable threshold (0.0 to 1.0). Detects speech segments and triggers transcription only when speech is detected followed by silence.

### 5.3 Authentication: Fernet Encryption

```python
from cryptography.fernet import Fernet

ENCRYPTION_KEY = "pbFK_uqlqLoNHuoiuuQZykzoeK5S8oEGh63pSwNfec0="
SECRET_API_KEY = "HuoiuuQZykzoeK5S8o"

cipher_suite = Fernet(ENCRYPTION_KEY.encode())
encrypted_key = cipher_suite.encrypt(SECRET_API_KEY.encode())
# Result: gAAAAABo-1oxslqx1hGc8nGn_7iWiD0jwAGE7tDk3MgA-t_9gM05qFZIP1tTiBgJpDkTaTrf7OHe9RLj2AjspUYuKxAqVnPjIJ6AD6q-0E8paCMBreZ8pGc=
```

REST uses `x-api-key` header. WebSocket uses `api_key` field in handshake JSON.

### 5.4 Other Endpoints

```text
GET  /              - health info
GET  /health-check  - health check
OPTIONS /{path:path} - CORS preflight
```

## 6. Hamsa Cloud API (from docs.tryhamsa.com)

The self-hosted instance has a different API surface than the Hamsa cloud platform. Here is what the cloud offers.

### 6.1 Cloud REST STT: POST /v1/realtime/stt

**Auth**: `Authorization: Token <api-key>` header.

**Request** (JSON body):

```json
{
  "audioBase64": "WAV-base64-string",
  "language": "ar",
  "isEosEnabled": false,
  "eosThreshold": 0.3
}
```

**Response**:

```json
{
  "text": "transcribed text"
}
```

Note: The cloud returns a simple `{"text": "..."}` while the self-hosted returns `{"text": "...", "gender": "...", "processing_time": ..., "duration": ...}`.

### 6.2 Cloud Async STT: POST /v1/jobs/transcribe

Async batch transcription from media URLs. Returns a `jobId`, results delivered via webhook. Not relevant for real-time use.

### 6.3 Cloud WebSocket API: wss://api.tryhamsa.com/v1/realtime/ws

**Auth**: `?api_key=YOUR_API_KEY` query parameter or `X-Api-Key` header.

**Message format** (unified for TTS and STT):

```typescript
interface WebSocketMessage {
  type: "tts" | "stt" | "response" | "error" | "info" | "ack" | "end";
  payload?: object;
}
```

**STT request** (client -> server):

```json
{
  "type": "stt",
  "payload": {
    "audioBase64": "base64-audio-data",
    "language": "ar",
    "isEosEnabled": true,
    "eosThreshold": 0.3
  }
}
```

**STT response** (server -> client): plain text string (not JSON):

```text
مرحبا بك في خدمة همسة
```

This is a request/response pattern (send complete audio, get text back), not a streaming VAD pattern. The cloud WebSocket STT is essentially the REST endpoint over WebSocket.

### 6.4 Key Difference: Cloud vs Self-Hosted

| Feature | Cloud API | Self-Hosted |
|---------|-----------|-------------|
| STT REST endpoint | `POST /v1/realtime/stt` | `POST /transcribe` |
| STT REST request | `{audioBase64, language}` | `{audio, lang, gender_detection, ...}` |
| STT REST response | `{text}` | `{text, gender, eos, processing_time, duration, ...}` |
| WebSocket endpoint | `wss://api.tryhamsa.com/v1/realtime/ws` | `ws://host:8080/ws` |
| WebSocket STT | Request/response (send full audio, get text) | Streaming VAD (send PCM chunks, get segment transcriptions) |
| WebSocket auth | `?api_key=` query param | `api_key` field in handshake JSON (Fernet-encrypted) |
| WebSocket message format | `{type: "stt", payload: {audioBase64, ...}}` | `{type: "handshake", api_key, options}` then binary PCM |
| VAD | Server-side, not exposed | Client streams PCM, server VAD triggers transcription |
| Gender detection | Not in cloud STT | Supported (returns gender per segment) |
| Speaker identification | Not in cloud STT | Supported (returns speaker_info) |
| Wake word | Not in cloud STT | Supported (wake_word_match, similarity_score) |
| Noise cancellation | Not in cloud STT | Supported (Krisp, configurable) |
| Dialect/language switcher | In voice agents, not raw STT | `lang: "auto"` |
| TTS | `POST /v1/realtime/tts-stream` | Not in self-hosted STT pod |

## 7. hamsa_livekit Plugin (from GitHub repo)

The `hamsa-ai/hamsa_livekit` repo is a LiveKit Agents plugin that wraps the **Hamsa cloud API** (not the self-hosted instance).

### 7.1 STT Module (stt.py)

```python
HAMSA_STT_BASE_URL = "https://api.tryhamsa.com/v1/realtime/stt"
```

Key observations from the source code:

- Uses `POST /v1/realtime/stt` (cloud REST endpoint, not WebSocket)
- Auth: `Authorization: Token {api_key}` header
- Sends `audioBase64` (WAV format, 16kHz) with `language`, `isEosEnabled`, `eosThreshold`
- Parses response: `response_json["data"]["text"]` (note: this expects `{success, data: {text}}` wrapper, which differs from the documented `{text}` response)
- Capabilities: `streaming=False, interim_results=False` (batch only, no streaming)
- Converts LiveKit `AudioBuffer` to WAV base64 via `rtc.combine_audio_frames(buffer).to_wav_bytes()`
- Hardcodes `confidence=1.0` and `gender="Male"` (Hamsa cloud doesn't return these)
- No VAD, no WebSocket, no real-time streaming

### 7.2 TTS Module (tts.py)

```python
BASE_URL = "https://api.tryhamsa.com/v1/realtime/tts-stream"
```

- Uses cloud TTS streaming endpoint
- Auth: `Authorization: Token {api_key}` header
- Sends `{speaker, dialect, text, mulaw}` as JSON
- Receives raw audio chunks (8192 bytes each) streamed back
- Supports 24+ Arabic voices across 9 dialects
- Sample rate: 16kHz default, configurable

### 7.3 What Can Be Reused

The LiveKit plugin targets the cloud API, which has a simpler protocol than our self-hosted instance. However:

- The STT module's request format (`audioBase64` + `language` + `isEosEnabled` + `eosThreshold`) is similar to what the self-hosted `/transcribe` accepts, just with different field names
- The TTS module is not applicable since our self-hosted instance is STT-only
- The cloud WebSocket protocol (`{type: "stt", payload: {audioBase64}}`) is a request/response pattern, fundamentally different from our self-hosted streaming VAD WebSocket

The LiveKit plugin code confirms that Hamsa's own cloud STT is a simple batch transcription API with no streaming, no VAD, no gender detection, and no speaker identification in the cloud STT product.

## 8. Functionality Loss Assessment: OpenAI Compatibility

### 8.1 OpenAI Transcription API (POST /v1/audio/transcriptions)

OpenAI's transcription API is a batch endpoint:

```text
POST /v1/audio/transcriptions
Content-Type: multipart/form-data

file: <audio file>
model: whisper-1
language: optional
prompt: optional
response_format: json (default), text, srt, verbose_json, vtt
temperature: 0-1
timestamp_granularities: segment, word
```

Response (json format):

```json
{
  "text": "transcribed text"
}
```

Response (verbose_json format):

```json
{
  "task": "transcribe",
  "language": "arabic",
  "duration": 29.83,
  "text": "full transcription",
  "segments": [...],
  "words": [...]
}
```

### 8.2 Mapping: Self-Hosted Hamsa to OpenAI Transcription

| OpenAI Field | Hamsa Self-Hosted | Status |
|-------------|-------------------|--------|
| `file` (multipart upload) | `audio` (base64 in JSON) | Adapter converts multipart file to base64 |
| `model` | Not applicable (single model) | Can be ignored or mapped to "hamsa-stt" |
| `language` | `lang` ("auto" or specific) | Direct mapping |
| `prompt` | `prompt` | Direct mapping |
| `response_format: json` | `{text}` from response | Direct mapping |
| `response_format: verbose_json` | Not supported | Would need to fabricate segments/duration |
| `response_format: text/srt/vtt` | Not supported | Would need to format output |
| `temperature` | Not supported | Ignored |
| `timestamp_granularities` | Not supported | Ignored |

**Functionality lost in REST adapter**:

- `gender` detection (Hamsa returns it, OpenAI format has no field for it)
- `eos` (end-of-speech) detection
- `processing_time` and `duration` (could be mapped to verbose_json `duration`)
- `speaker_embeddings` and `speaker_identification`
- `wake_word` detection and `similarity_score`

### 8.3 OpenAI Realtime API (WebSocket)

OpenAI's Realtime API is a bidirectional event-driven protocol for full voice conversations (STT + LLM + TTS in one session). Key events:

```text
Client -> Server: session.update (configure modalities, voice, turn detection)
Client -> Server: input_audio_buffer.append (base64 PCM chunks)
Client -> Server: input_audio_buffer.commit
Client -> Server: response.create
Server -> Client: session.created
Server -> Client: conversation.item.created
Server -> Client: response.audio_transcript.delta (streaming text)
Server -> Client: response.audio.delta (streaming TTS audio)
Server -> Client: response.done
```

### 8.4 Mapping: Self-Hosted Hamsa WebSocket to OpenAI Realtime

| OpenAI Realtime | Hamsa WebSocket | Status |
|----------------|-----------------|--------|
| `session.update` | `handshake` with options | Partial: Hamsa options map to some session config |
| `input_audio_buffer.append` (base64) | Binary PCM chunks | Adapter must decode base64 to binary PCM |
| `input_audio_buffer.commit` | Not needed (VAD auto-detects) | Different paradigm |
| `response.create` | Not needed (VAD auto-triggers) | Different paradigm |
| `response.audio_transcript.delta` | `transcription` message | Adapter translates format |
| `response.audio.delta` (TTS) | Not supported | Hamsa STT pod has no TTS |
| Turn detection (server VAD) | Silero VAD | Both use VAD, but different implementations |
| Interruption handling | Not supported | Would need adapter logic |
| Function calling / tools | Not supported | Not applicable to STT-only |
| Multiple modalities (text + audio) | STT only | Hamsa STT pod has no TTS/LLM |

**Functionality lost in WebSocket adapter**:

- `gender` detection per transcription segment
- `eos` (end-of-speech) prediction per segment
- `speaker_info` and speaker identification
- Configurable VAD parameters (`vad_threshold`, `min_speech_ms`, `min_silence_duration_ms`, `silence_timeout`) - OpenAI Realtime exposes server VAD config but with different parameter names and semantics
- `noise_cancellation` (Krisp) - OpenAI has its own noise cancellation
- `audio_type` selection (PCM vs MULAW)
- Real-time segment-level transcription (Hamsa transcribes per VAD-detected segment; OpenAI streams token-level deltas)

**Functionality that cannot be expressed in OpenAI format at all**:

- `gender` field (no OpenAI equivalent)
- `speaker_embeddings` (no OpenAI equivalent)
- `wake_word_match` and `similarity_score` (no OpenAI equivalent)
- `eos.prediction` (OpenAI has `turn_detection` but the semantics differ)

### 8.5 Summary: What Hamsa Would Lose

If we make Hamsa fully OpenAI-compatible, these self-hosted features have no OpenAI API equivalent and would be inaccessible through the OpenAI-compatible interface:

1. **Gender detection** - Hamsa returns `gender: "Male"/"Female"` per transcription segment. OpenAI has no gender field.
2. **Speaker identification** - Hamsa can identify and return speaker info. OpenAI has no speaker ID in transcription.
3. **Speaker embeddings** - Hamsa returns embedding vectors. OpenAI has no equivalent.
4. **Wake word detection** - Hamsa can detect wake words and return similarity scores. OpenAI has no equivalent.
5. **EOS (end-of-speech) prediction** - Hamsa returns a prediction score per segment. OpenAI's turn detection is similar but not exposed as a per-segment value.
6. **Configurable VAD parameters** - Hamsa exposes `vad_threshold`, `min_speech_ms`, `min_silence_duration_ms`, `silence_timeout`. OpenAI Realtime exposes some VAD config but with different semantics.
7. **Noise cancellation** - Hamsa supports Krisp. OpenAI has its own solution but it's not configurable through the same parameters.
8. **MULAW audio support** - Hamsa accepts MULAW 8kHz. OpenAI uses PCM 24kHz.

**Mitigation**: These features could be preserved by keeping the native Hamsa WebSocket endpoint (`/v1/hamsa/stt/ws`) alongside any OpenAI-compatible endpoint. Clients who need gender detection, speaker ID, or wake word would use the native endpoint. Clients who just need transcription text would use the OpenAI-compatible endpoint.

## 9. Architecture Recommendation

### 9.1 REST: OpenAI-Compatible Adapter (Low Effort)

Create a provider adapter that maps `POST /v1/audio/transcriptions` to Hamsa's `POST /transcribe`:

```text
Client (OpenAI SDK) -> LiteLLM /v1/audio/transcriptions -> Hamsa /transcribe
                         (multipart -> JSON base64)
                         (OpenAI response <- Hamsa response)
```

This gives full OpenAI transcription compatibility. Gender/eos/speaker data is dropped (no OpenAI field for it).

### 9.2 WebSocket: Keep Native Endpoint (No OpenAI Realtime Mapping)

The self-hosted WebSocket protocol is fundamentally different from OpenAI Realtime:

- Hamsa: streaming PCM in, VAD-triggered segment transcriptions out
- OpenAI Realtime: full conversation protocol (STT + LLM + TTS) with event-driven messages

Mapping Hamsa STT-only WebSocket to OpenAI Realtime would lose the TTS and LLM half of the Realtime API, making it a broken implementation. The native `/v1/hamsa/stt/ws` endpoint (via the existing startup hook) is the correct approach for streaming STT.

### 9.3 Hybrid Approach (Recommended)

Keep both endpoints:

1. `POST /v1/audio/transcriptions` - OpenAI-compatible REST (new adapter)
2. `WS /v1/hamsa/stt/ws` - Native Hamsa WebSocket (existing hook, preserves all features)

This gives OpenAI SDK compatibility for batch transcription while preserving the full feature set for real-time streaming use cases.

## 10. Network Topology and Internal vs External Access

### 10.1 Internal Cluster Service (REST and WS both work)

```text
Service:  s-9c57bce9-0583-4bf7-9443-08825220a231 (ClusterIP, namespace: adeo)
REST URL: http://s-9c57bce9-0583-4bf7-9443-08825220a231.adeo.svc.cluster.local:8080/transcribe
WS URL:   ws://s-9c57bce9-0583-4bf7-9443-08825220a231.adeo.svc.cluster.local:8080/ws
```

The internal service accepts:
- REST: `x-api-key` header with Fernet-encrypted key. Returns 200 with transcription JSON.
- WS: `api_key` field in handshake JSON. Returns `handshake_ack` with `status: "authenticated"`.

### 10.2 External Inference Proxy (REST broken, WS broken)

```text
REST URL: https://inference.adeoaiengine.ecouncil.ae/models/9c57bce9-0583-4bf7-9443-08825220a231/proxy/transcribe
WS URL:   wss://inference.adeoaiengine.ecouncil.ae/models/9c57bce9-0583-4bf7-9443-08825220a231/ws/ws
```

The external inference proxy at `inference.adeoaiengine.ecouncil.ae` has its own auth layer that is incompatible with the Hamsa Fernet key:
- REST with `x-api-key` header: returns `{"detail":"Authorization header is missing","status_code":401}`
- REST with `Authorization: Bearer <key>`: returns `{"detail":"Invalid API key","status_code":401}`
- REST with `Authorization: Token <key>`: returns `{"detail":"Invalid Authorization header","status_code":401}`
- REST with `Authorization: <key>` (no prefix): returns `{"detail":"Invalid Authorization header","status_code":401}`
- WS: closes with `1008 (policy violation) Missing token in auth message`

### 10.3 Internal Service Path Discovery

The internal service has different paths for REST vs WS:
- `/transcribe` (REST POST) - works, returns transcription JSON
- `/ws` (WebSocket) - works, returns `handshake_ack`
- `/ws/ws` (WebSocket) - returns HTTP 403
- Bare `:8080` or `:8080/` (WebSocket) - returns HTTP 403

### 10.4 Protocol Conversion

When the model is registered with a `wss://` or `ws://` api_base (as is natural for a WS-based service), REST calls need protocol conversion:
- `ws://` to `http://` for REST
- `wss://` to `https://` for REST (with SSL verification disabled for internal certs)
- Keep `ws://` or `wss://` for WS

### 10.5 SSL Considerations

The internal cluster service uses HTTP (no TLS), so no SSL context needed. If using the external `wss://` URL, SSL certificate verification fails because the cluster uses internal/self-signed certs. Disable verification with:
```python
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
```

## 11. LiteLLM Integration Findings

### 11.1 Model Registration

The model is registered in the LiteLLM DB with:
```json
{
  "model_name": "tryhamsa-stt",
  "litellm_params": {
    "api_base": "http://s-9c57bce9-0583-4bf7-9443-08825220a231.adeo.svc.cluster.local:8080",
    "custom_llm_provider": "custom",
    "use_in_pass_through": true,
    "model": "custom/tryhamsa-stt"
  },
  "model_info": {
    "mode": "audio_transcription",
    "description": "Hamsa STT WebSocket-based real-time transcription service"
  }
}
```

Key fields:
- `use_in_pass_through: true` - enables `get_available_deployment_for_pass_through()` to resolve this model
- `custom_llm_provider: "custom"` - marks it as a custom provider, not a known LLM provider
- `mode: "audio_transcription"` - tags the call type for logging
- `api_base` - the internal cluster IP URL (no path suffix; routes are appended by the provider code)

### 11.2 LiteLLM Router: get_available_deployment_for_pass_through

```python
from litellm.proxy.proxy_server import llm_router

deployment = llm_router.get_available_deployment_for_pass_through(model="tryhamsa-stt")
api_base = deployment.get("litellm_params", {}).get("api_base", "")
```

This applies native RPM/TPM/priority/cooldown/load-balancing and returns the deployment dict. Raises `RouterRateLimitError` (from `litellm.types.router`) if rate limits are exceeded. The error has a `.cooldown_time` attribute (seconds) that should map to HTTP 429 `retry-after` for REST and WS close code 1013 for WebSocket.

### 11.3 LiteLLM Built-in Pass-Through Endpoints

LiteLLM has a built-in generic pass-through system (`pass_through_endpoints` in `general_settings` or DB):

```yaml
general_settings:
  pass_through_endpoints:
    - path: /hamsa
      target: http://s-9c57bce9-0583-4bf7-9443-08825220a231.adeo.svc.cluster.local:8080
      headers:
        x-api-key: "gAAAAAB..."
      include_subpath: true
      auth: true
```

With `include_subpath: true`:
- `POST /hamsa/transcribe` forwards to `http://s-...:8080/transcribe`
- `POST /hamsa/any-route` forwards to `http://s-...:8080/any-route`

**Limitations of the built-in system for Hamsa:**
1. The `target` is static. It does NOT resolve from the LiteLLM model registry, so no RPM/TPM/priority/cooldown/load-balancing from the router.
2. WebSocket pass-through exists (`create_websocket_passthrough_route`) but the config-based registration only registers HTTP routes, not WS.
3. No support for Hamsa's handshake `api_key` injection (the Fernet key must be injected into the first JSON message, not sent as an HTTP header).

### 11.4 LiteLLM LLM Provider Pass-Through

LiteLLM has provider-specific pass-through endpoints (`/anthropic/{endpoint:path}`, `/cohere/{endpoint:path}`, `/gemini/{endpoint:path}`). These resolve the provider's `api_base` from provider config and forward arbitrary subpaths. But this is hardcoded to known providers via `ProviderConfigManager.get_provider_model_info()`. There is no generic "custom provider" version.

### 11.5 Recommended Architecture: Prefix-Scoped Catch-All

Register a prefix-scoped catch-all route (e.g. `/tryhamsa/{endpoint:path}`) that:
1. Reads `model` from query param or body
2. Calls `get_available_deployment_for_pass_through(model)` for RPM/TPM/priority
3. Forwards the captured subpath to `api_base + subpath`

This avoids route conflicts with existing LiteLLM routes, provides model-registry-based rate limiting, and doesn't hardcode route paths like `/transcribe` or `/ws`.

**REST flow:**
```
POST /tryhamsa/transcribe?model=tryhamsa-stt
  -> user_api_key_auth
  -> get_available_deployment_for_pass_through("tryhamsa-stt")
  -> forward to api_base + "/transcribe" with x-api-key header
```

**WS flow (requires handshake injection):**
```
WS /tryhamsa/ws?model=tryhamsa-stt
  -> user_api_key_auth_websocket (via subprotocol)
  -> get_available_deployment_for_pass_through("tryhamsa-stt")
  -> forward to ws_url(api_base) + "/ws"
  -> inject Fernet key into first handshake JSON message
```

## 12. Verified Test Results

### 12.1 REST Transcription (internal cluster URL)

```bash
AUDIO_B64=$(base64 -w0 hello.wav)
curl -sk -X POST https://litellm.ecouncil.ae/custom/audio/transcriptions \
  -H "Authorization: Bearer {{ master_key }}" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"tryhamsa-stt\",\"audio\":\"$AUDIO_B64\",\"eos_enabled\":false,\"eos_threshold\":0.3,\"lang\":\"auto\"}"
```

Response:
```json
{
  "text": " Hello.",
  "gender": "Female",
  "eos": null,
  "processing_time": 0.11553049087524414,
  "speaker_embeddings": null,
  "duration": 0.72,
  "wake_word_match": null,
  "similarity_score": null
}
```

### 12.2 WebSocket Transcription (internal cluster URL)

Tested from inside the LiteLLM pod with real audio (hello.wav, 16kHz, 16-bit, mono, 11520 frames):

```text
Connecting to ws://localhost:4000/custom/realtime?model=tryhamsa-stt
Connected!
Sent handshake
Handshake response: {"type":"handshake_ack","status":"authenticated","message":"Ready to receive audio"}
Sent 23040 bytes of audio in 8 chunks (3200 bytes/chunk, 100ms pacing)
Sent EOS config
Received [0]: {"type":"transcription","data":{"transcription":" Hello.","gender":"Female","eos":{"prediction":0,"probability":0.1442742496728897},"speaker_info":null,"language":"en"},"duration_ms":198.99021834135056}
```

### 12.3 Key Behavioral Notes

- Audio must be sent as **raw binary PCM** (16-bit signed, 16kHz, mono), not base64, over WebSocket
- Chunk size of 3200 bytes (100ms at 16kHz 16-bit mono) with 100ms pacing between chunks is required for VAD to trigger
- Dumping all chunks at once without pacing produces zero transcriptions
- The handshake `api_key` field is overwritten by the gateway with the real Fernet key before forwarding to the upstream; clients can send a placeholder value
- WebSocket auth from browser uses `sec-websocket-protocol: openai-insecure-api-key.<litellm-key>` subprotocol (same as OpenAI Realtime API pattern)

## 13. API Key

```text
Fernet-encrypted key: gAAAAABo-1oxslqx1hGc8nGn_7iWiD0jwAGE7tDk3MgA-t_9gM05qFZIP1tTiBgJpDkTaTrf7OHe9RLj2AjspUYuKxAqVnPjIJ6AD6q-0E8paCMBreZ8pGc=
```

Note the `M05qF` segment in the middle. This is the key used for both REST (`x-api-key` header) and WS (`api_key` field in handshake JSON). Can be overridden via `HAMSA_STT_UPSTREAM_API_KEY` environment variable.
