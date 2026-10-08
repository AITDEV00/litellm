
# OmniVoice Server API Reference

**Version:** 0.2.4
**Base URL:** `http://<host>:8080`
**Auth:** Bearer token (optional, configured via `OMNIVOICE_API_KEY`)

## Table of Contents

- [Health &amp; Metrics](#health-metrics)
  - [GET /health](#get-health)
  - [GET /health-check](#get-health-check)
  - [GET /metrics](#get-metrics)
- [Models](#models)
  - [GET /v1/models](#get-v1models)
  - [GET /v1/models/{model_id}](#get-v1models_1)
- [Voices](#voices)
  - [GET /v1/voices](#get-v1voices)
  - [POST /v1/voices/profiles](#post-v1voicesprofiles)
  - [GET /v1/voices/profiles/{profile_id}](#get-v1voicesprofiles)
  - [PATCH /v1/voices/profiles/{profile_id}](#patch-v1voicesprofiles)
  - [DELETE /v1/voices/profiles/{profile_id}](#delete-v1voicesprofiles)
- [Speech Synthesis](#speech-synthesis)
  - [POST /v1/audio/speech](#post-v1audiospeech)
  - [POST /v1/audio/speech/clone](#post-v1audiospeechclone)
- [Script Synthesis](#script-synthesis)
  - [POST /v1/audio/script](#post-v1audioscript)
- [Voice Reference](#voice-reference)
  - [Presets](#presets)
  - [Design Attributes](#design-attributes)
- [Error Handling](#error-handling)

---

## Health & Metrics

### GET /health

Readiness probe. Returns `503` while the model is loading, `200` when ready.

```bash
curl http://localhost:8080/health
```

**Response (200):**

```json
{
  "status": "healthy",
  "ready": true,
  "model_loaded": true,
  "uptime_s": 304.5,
  "model_id": "/home/runner/models/OmniVoice",
  "memory_rss_mb": 1849.8
}
```

**Response (503 — model loading):**

```json
{
  "status": "starting",
  "ready": false,
  "model_loaded": false,
  "memory_rss_mb": 780.3
}
```

---

### GET /health-check

Alias for [`GET /health`](#get-health). Identical response. Useful for load balancers or K8s probes that expect a different path.

```bash
curl http://localhost:8080/health-check
```

---

### GET /metrics

Returns request counters, latency percentiles, and memory usage.

```bash
curl http://localhost:8080/metrics
```

**Response (200):**

```json
{
  "requests_total": 42,
  "requests_success": 40,
  "requests_error": 2,
  "requests_timeout": 0,
  "mean_latency_ms": 312.5,
  "p95_latency_ms": 480.0,
  "ram_mb": 1850.2,
  "script_requests_total": 3,
  "script_requests_success": 3,
  "script_segments_synthesized": 7
}
```

---

## Models

### GET /v1/models

OpenAI-compatible model listing. Returns all model IDs accepted by the `/v1/audio/speech` endpoint.

```bash
curl http://localhost:8080/v1/models
```

**Response (200):**

```json
{
  "object": "list",
  "data": [
    {
      "id": "omnivoice",
      "object": "model",
      "created": 1782834444,
      "owned_by": "k2-fsa",
      "permission": [],
      "root": "/home/runner/models/OmniVoice",
      "parent": null
    },
    {
      "id": "tts-1",
      "object": "model",
      "created": 1782834444,
      "owned_by": "k2-fsa",
      "permission": [],
      "root": "/home/runner/models/OmniVoice",
      "parent": "omnivoice"
    },
    {
      "id": "tts-1-hd",
      "object": "model",
      "created": 1782834444,
      "owned_by": "k2-fsa",
      "permission": [],
      "root": "/home/runner/models/OmniVoice",
      "parent": "omnivoice"
    }
  ]
}
```

> `omnivoice`, `tts-1`, and `tts-1-hd` all map to the same underlying model. The aliases exist for OpenAI SDK drop-in compatibility.

---

### GET /v1/models/

Retrieve details for a specific model.

```bash
curl http://localhost:8080/v1/models/omnivoice
```

**Path Parameters:**

| Parameter    | Type   | Required | Description                                   |
| ------------ | ------ | -------- | --------------------------------------------- |
| `model_id` | string | Yes      | One of:`omnivoice`, `tts-1`, `tts-1-hd` |

**Response (200):**

```json
{
  "id": "omnivoice",
  "object": "model",
  "created": 1782834444,
  "owned_by": "k2-fsa",
  "permission": [],
  "root": "/home/runner/models/OmniVoice",
  "parent": null
}
```

**Response (404):**

```json
{
  "detail": "Model 'gpt-4' not found. Available: ['omnivoice', 'tts-1', 'tts-1-hd']"
}
```

---

## Voices

### GET /v1/voices

List all available voices: built-in presets, design attribute options, and saved clone profiles.

```bash
curl http://localhost:8080/v1/voices
```

**Response (200):**

```json
{
  "voices": [
    {
      "id": "auto",
      "type": "auto",
      "description": "Fallback/default prompt when no instructions or recognized preset is provided: male, middle-aged, moderate pitch, british accent"
    },
    {
      "id": "design:<attributes>",
      "type": "design",
      "description": "Voice design via attributes. Example: 'design:female,british accent'"
    },
    {
      "id": "alloy",
      "type": "preset",
      "description": "OpenAI-compatible preset mapped to 'female, young adult, moderate pitch, american accent'"
    },
    {
      "id": "clone:my_voice",
      "type": "clone",
      "profile_id": "my_voice",
      "created_at": "2026-06-30T15:47:39.821569+00:00",
      "ref_text": "Hello, this is a test of the OmniVoice text-to-speech system."
    }
  ],
  "design_attributes": {
    "gender": ["male", "female"],
    "age": ["child", "teenager", "young adult", "middle-aged", "elderly"],
    "pitch": ["very low pitch", "low pitch", "moderate pitch", "high pitch", "very high pitch"],
    "style": ["whisper"],
    "accent_en": ["american accent", "british accent", "australian accent", "canadian accent", "..."],
    "dialect_zh": ["河南话", "陕西话", "四川话", "..."]
  },
  "total": 15
}
```

---

### POST /v1/voices/profiles

Create a voice cloning profile from reference audio. The profile can later be used via `voice: "clone:<profile_id>"` in `/v1/audio/speech`.

**Content-Type:** `multipart/form-data`

```bash
curl -X POST http://localhost:8080/v1/voices/profiles \
  -F "profile_id=my_voice" \
  -F "ref_audio=@reference_audio.wav" \
  -F "ref_text=The text spoken in the reference audio." \
  -F "overwrite=true"
```

**Form Parameters:**

| Parameter      | Type   | Required | Default   | Description                                                                                    |
| -------------- | ------ | -------- | --------- | ---------------------------------------------------------------------------------------------- |
| `profile_id` | string | Yes      | —        | Unique ID. Alphanumeric, dashes, underscores. Max 64 chars.                                    |
| `ref_audio`  | file   | Yes      | —        | Reference audio file (WAV/MP3/FLAC). Max 25MB.                                                 |
| `ref_text`   | string | No       | `null`  | Transcript of the reference audio. If omitted, Whisper ASR auto-transcribes at synthesis time. |
| `overwrite`  | bool   | No       | `false` | Overwrite if profile already exists.                                                           |

**Response (201):**

```json
{
  "profile_id": "my_voice",
  "name": "my_voice",
  "ref_text": "The text spoken in the reference audio.",
  "created_at": "2026-06-30T15:47:39.821569+00:00"
}
```

**Response (409 — already exists):**

```json
{
  "detail": "Profile 'my_voice' already exists. Use overwrite=true to replace."
}
```

---

### GET /v1/voices/profiles/

Retrieve metadata for a specific clone profile.

```bash
curl http://localhost:8080/v1/voices/profiles/my_voice
```

**Path Parameters:**

| Parameter      | Type   | Required | Description            |
| -------------- | ------ | -------- | ---------------------- |
| `profile_id` | string | Yes      | The profile identifier |

**Response (200):**

```json
{
  "profile_id": "my_voice",
  "name": "my_voice",
  "ref_text": "The text spoken in the reference audio.",
  "created_at": "2026-06-30T15:47:39.821569+00:00"
}
```

**Response (404):**

```json
{
  "detail": "Profile 'my_voice' not found"
}
```

---

### PATCH /v1/voices/profiles/

Update an existing profile's reference audio and/or reference text. Fields not provided are left unchanged.

**Content-Type:** `multipart/form-data`

```bash
# Update ref_text only
curl -X PATCH http://localhost:8080/v1/voices/profiles/my_voice \
  -F "ref_text=Updated reference text."

# Update ref_audio only
curl -X PATCH http://localhost:8080/v1/voices/profiles/my_voice \
  -F "ref_audio=@new_reference.wav"

# Update both
curl -X PATCH http://localhost:8080/v1/voices/profiles/my_voice \
  -F "ref_audio=@new_reference.wav" \
  -F "ref_text=Updated text matching new audio."
```

**Form Parameters:**

| Parameter     | Type   | Required | Default  | Description                                              |
| ------------- | ------ | -------- | -------- | -------------------------------------------------------- |
| `ref_audio` | file   | No       | `null` | New reference audio. If omitted, existing audio is kept. |
| `ref_text`  | string | No       | `null` | New reference text. If omitted, existing text is kept.   |

> At least one of `ref_audio` or `ref_text` must be provided.

**Response (200):**

```json
{
  "profile_id": "my_voice",
  "name": "my_voice",
  "ref_text": "Updated reference text.",
  "created_at": "2026-06-30T15:47:39.821569+00:00"
}
```

---

### DELETE /v1/voices/profiles/

Delete a clone profile and its stored reference audio.

```bash
curl -X DELETE http://localhost:8080/v1/voices/profiles/my_voice
```

**Response (204):** No content (success)

**Response (404):**

```json
{
  "detail": "Profile 'my_voice' not found"
}
```

---

## Speech Synthesis

### POST /v1/audio/speech

Generate speech from text. Supports design voices, OpenAI presets, clone profiles, and streaming.

**Content-Type:** `application/json`

#### Basic synthesis (default voice)

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "omnivoice",
    "input": "Hello, this is a test of the OmniVoice text-to-speech system.",
    "response_format": "wav"
  }' \
  --output output.wav
```

#### Design voice (attribute-based)

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "omnivoice",
    "input": "This voice has been designed with specific attributes.",
    "instructions": "female,british accent,young adult,high pitch",
    "response_format": "wav"
  }' \
  --output output.wav
```

#### Preset voice

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "omnivoice",
    "input": "Testing the nova preset voice.",
    "voice": "nova",
    "response_format": "wav"
  }' \
  --output output.wav
```

#### Clone via stored profile

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "omnivoice",
    "input": "This uses a stored clone profile for voice cloning.",
    "voice": "clone:my_voice",
    "response_format": "wav"
  }' \
  --output output.wav
```

#### Streaming (PCM chunks)

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "omnivoice",
    "input": "This is a longer text that will be streamed in chunks. Each sentence is synthesized and sent as soon as it is ready.",
    "stream": true,
    "response_format": "pcm",
    "position_temperature": 0.0
  }' \
  --output output.pcm
```

> Streaming requires `response_format: "pcm"`. Set `position_temperature: 0.0` for consistent voice across chunks. Convert to WAV afterwards:
> `ffmpeg -f s16le -ar 24000 -ac 1 -i output.pcm output.wav`

#### Advanced generation parameters

```bash
curl -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "model": "omnivoice",
    "input": "This synthesis uses advanced generation parameters for quality tuning.",
    "instructions": "female,american accent",
    "num_step": 32,
    "guidance_scale": 3.5,
    "position_temperature": 0.0,
    "layer_penalty_factor": 0.5,
    "response_format": "wav"
  }' \
  --output output.wav
```

**Request Body:**

| Parameter                | Type   | Required | Default                 | Range                                                  | Description                                                                     |
| ------------------------ | ------ | -------- | ----------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------- |
| `model`                | string | No       | `"omnivoice"`         | `omnivoice`, `tts-1`, `tts-1-hd`                 | Model alias (all map to same engine)                                            |
| `input`                | string | Yes      | —                      | 1–10,000 chars                                        | Text to synthesize                                                              |
| `voice`                | string | No       | `"auto"`              | See[Voice Reference](#voice-reference)                  | Voice selector: preset name,`clone:<id>`, `design:<attrs>`, or `auto`     |
| `instructions`         | string | No       | `null`                | Design attributes                                      | Voice design attributes (e.g.`"female,british accent"`)                       |
| `response_format`      | string | No       | `"wav"`               | `mp3`, `opus`, `aac`, `flac`, `wav`, `pcm` | Output audio format                                                             |
| `speed`                | float  | No       | `1.0`                 | 0.25–4.0                                              | Speech speed multiplier                                                         |
| `stream`               | bool   | No       | `false`               | —                                                     | Enable sentence-level PCM streaming                                             |
| `num_step`             | int    | No       | `32` (server default) | 1–64                                                  | Diffusion steps. Higher = better quality, slower.                               |
| `guidance_scale`       | float  | No       | server default          | 0.0–10.0                                              | CFG scale. Higher = stronger voice conditioning.                                |
| `denoise`              | bool   | No       | server default          | —                                                     | Enable upstream denoising token                                                 |
| `t_shift`              | float  | No       | server default          | 0.0–2.0                                               | Noise schedule shift                                                            |
| `position_temperature` | float  | No       | server default          | 0.0–10.0                                              | Temperature for mask-position selection. 0 = deterministic.                     |
| `class_temperature`    | float  | No       | server default          | 0.0–2.0                                               | Temperature for token sampling. 0 = greedy.                                     |
| `duration`             | float  | No       | `null`                | 0.1–60.0                                              | Target audio duration in seconds                                                |
| `language`             | string | No       | `null`                | Language code                                          | Language code (e.g.`"en"`, `"vi"`, `"zh"`) for multilingual pronunciation |
| `layer_penalty_factor` | float  | No       | `null`                | ≥0.0                                                  | Layer penalty factor for quality tuning                                         |
| `preprocess_prompt`    | bool   | No       | `null`                | —                                                     | Preprocess the input prompt                                                     |
| `postprocess_output`   | bool   | No       | `null`                | —                                                     | Postprocess the output audio                                                    |
| `request_timeout_s`    | int    | No       | server default          | 1–600                                                 | Per-request timeout in seconds                                                  |

**Response Headers:**

| Header                    | Description                              |
| ------------------------- | ---------------------------------------- |
| `X-Audio-Duration-S`    | Generated audio duration in seconds      |
| `X-Synthesis-Latency-S` | Total model inference latency in seconds |

**Response Body:** Binary audio data in the requested format.

---

### POST /v1/audio/speech/clone

One-shot voice cloning. Upload reference audio directly — no profile needed.

**Content-Type:** `multipart/form-data`

#### With reference text (fast — no Whisper needed)

```bash
curl -X POST http://localhost:8080/v1/audio/speech/clone \
  -F "text=This is one-shot voice cloning without saving a profile." \
  -F "ref_audio=@reference_audio.wav" \
  -F "ref_text=The text spoken in the reference audio." \
  -F "speed=1.0" \
  --output output.wav
```

#### Without reference text (slower — Whisper ASR auto-transcribes)

```bash
curl -X POST http://localhost:8080/v1/audio/speech/clone \
  -F "text=This is voice cloning with automatic transcription." \
  -F "ref_audio=@reference_audio.wav" \
  -F "speed=1.0" \
  --output output.wav
```

> When `ref_text` is omitted, the server uses Whisper ASR to transcribe the reference audio automatically. This adds ~0.3–0.5s latency.

#### Streaming clone

```bash
curl -X POST http://localhost:8080/v1/audio/speech/clone \
  -F "text=Long text to be streamed in sentence-level chunks..." \
  -F "ref_audio=@reference_audio.wav" \
  -F "ref_text=Reference text." \
  -F "stream=true" \
  -F "response_format=pcm" \
  --output output.pcm
```

**Form Parameters:**

| Parameter             | Type   | Required | Default        | Range                                                  | Description                                                              |
| --------------------- | ------ | -------- | -------------- | ------------------------------------------------------ | ------------------------------------------------------------------------ |
| `text`              | string | Yes      | —             | 1–10,000 chars                                        | Text to synthesize                                                       |
| `ref_audio`         | file   | Yes      | —             | Max 25MB                                               | Reference audio file                                                     |
| `ref_text`          | string | No       | `null`       | —                                                     | Transcript of reference audio. If omitted, Whisper ASR auto-transcribes. |
| `response_format`   | string | No       | `"wav"`      | `mp3`, `opus`, `aac`, `flac`, `wav`, `pcm` | Output audio format                                                      |
| `stream`            | bool   | No       | `false`      | —                                                     | Enable sentence-level PCM streaming                                      |
| `speed`             | float  | No       | `1.0`        | 0.25–4.0                                              | Speech speed multiplier                                                  |
| `num_step`          | int    | No       | server default | 1–64                                                  | Diffusion steps                                                          |
| `guidance_scale`    | float  | No       | server default | 0.0–10.0                                              | CFG scale                                                                |
| `language`          | string | No       | `null`       | Language code                                          | Language for multilingual pronunciation                                  |
| `duration`          | float  | No       | `null`       | 0.1–60.0                                              | Target audio duration in seconds                                         |
| `request_timeout_s` | int    | No       | server default | 1–600                                                 | Per-request timeout                                                      |

> Advanced parameters (`denoise`, `t_shift`, `position_temperature`, `class_temperature`, `layer_penalty_factor`, `preprocess_prompt`, `postprocess_output`, `audio_chunk_duration`, `audio_chunk_threshold`) are also accepted — see [POST /v1/audio/speech](#post-v1audiospeech) for details.

**Response:** Same as `/v1/audio/speech` — binary audio with `X-Audio-Duration-S` and `X-Synthesis-Latency-S` headers.

---

## Script Synthesis

### POST /v1/audio/script

Multi-speaker script synthesis with voice resolution, pause insertion, and error handling.

**Content-Type:** `application/json`

#### Single-track output (mixed WAV)

Produces a single mixed audio file with pauses between speaker changes.

```bash
curl -X POST http://localhost:8080/v1/audio/script \
  -H "Content-Type: application/json" \
  -d '{
    "script": [
      {"speaker": "narrator", "text": "Welcome to the OmniVoice script synthesis demo.", "voice": "openai:onyx"},
      {"speaker": "alice", "text": "Hello! I am Alice, and I will be your guide today.", "voice": "openai:nova"},
      {"speaker": "narrator", "text": "Thank you Alice. Let us begin.", "voice": "openai:onyx"}
    ],
    "default_voice": "openai:onyx",
    "speed": 1.0,
    "on_error": "abort",
    "pause_between_speakers": 0.3,
    "response_format": "wav",
    "output_format": "single_track"
  }' \
  --output script.wav
```

#### Multi-track output (per-speaker WAV, base64-encoded JSON)

Returns each speaker's audio as a separate base64-encoded WAV track with segment timestamps.

```bash
curl -X POST http://localhost:8080/v1/audio/script \
  -H "Content-Type: application/json" \
  -d '{
    "script": [
      {"speaker": "narrator", "text": "Welcome to the demo.", "voice": "openai:onyx"},
      {"speaker": "alice", "text": "Hello! I am Alice.", "voice": "openai:nova"}
    ],
    "default_voice": "openai:onyx",
    "speed": 1.0,
    "on_error": "abort",
    "pause_between_speakers": 0.3,
    "output_format": "multi_track"
  }' \
  --output script.json
```

**Response (multi_track, 200):**

```json
{
  "tracks": {
    "narrator": "<base64 WAV>",
    "alice": "<base64 WAV>"
  },
  "metadata": {
    "total_duration_s": 4.523,
    "speakers_unique": 2,
    "segment_count": 2,
    "skipped_segments": [],
    "segments": [
      {"index": 0, "speaker": "narrator", "offset_s": 0.0, "duration_s": 1.812},
      {"index": 1, "speaker": "alice", "offset_s": 2.112, "duration_s": 1.411}
    ]
  }
}
```

#### Using clone profiles in scripts

```bash
curl -X POST http://localhost:8080/v1/audio/script \
  -H "Content-Type: application/json" \
  -d '{
    "script": [
      {"speaker": "host", "text": "Welcome to our podcast.", "voice": "clone:host_voice"},
      {"speaker": "guest", "text": "Thank you for having me.", "voice": "clone:guest_voice"}
    ],
    "default_voice": "clone:host_voice",
    "speed": 1.0,
    "on_error": "skip",
    "pause_between_speakers": 0.5,
    "output_format": "single_track",
    "response_format": "wav"
  }' \
  --output podcast.wav
```

#### Error handling: skip vs abort

```bash
# "skip" — continue on segment failure, list skipped indices
curl -X POST http://localhost:8080/v1/audio/script \
  -H "Content-Type: application/json" \
  -d '{
    "script": [...],
    "on_error": "skip",
    "output_format": "single_track"
  }'

# "abort" — stop on first segment failure (default)
```

**Request Body:**

| Parameter                  | Type   | Required | Default            | Range                                                  | Description                                          |
| -------------------------- | ------ | -------- | ------------------ | ------------------------------------------------------ | ---------------------------------------------------- |
| `script`                 | array  | Yes      | —                 | 1–100 segments                                        | Array of segment objects                             |
| `default_voice`          | string | No       | `null`           | Voice ID                                               | Default voice for segments without explicit`voice` |
| `speed`                  | float  | No       | `1.0`            | 0.25–4.0                                              | Speech speed multiplier (applies to all segments)    |
| `response_format`        | string | No       | `"wav"`          | `mp3`, `opus`, `aac`, `flac`, `wav`, `pcm` | Audio format (single_track only)                     |
| `output_format`          | string | No       | `"single_track"` | `single_track`, `multi_track`                      | Output mode                                          |
| `pause_between_speakers` | float  | No       | `0.5`            | 0.0–5.0                                               | Silence (seconds) inserted on speaker change         |
| `on_error`               | string | No       | `"abort"`        | `abort`, `skip`                                    | Error handling strategy                              |

**Segment Object:**

| Field       | Type   | Required | Default             | Range                         | Description                |
| ----------- | ------ | -------- | ------------------- | ----------------------------- | -------------------------- |
| `speaker` | string | Yes      | —                  | 1–64 chars,`[a-zA-Z0-9_-]` | Speaker identifier         |
| `text`    | string | Yes      | —                  | 1–10,000 chars               | Text to synthesize         |
| `voice`   | string | No       | `null`            | Voice ID                      | Per-segment voice override |
| `speed`   | float  | No       | `null` (inherits) | 0.25–4.0                     | Per-segment speed override |

**Constraints:**

- Max 100 segments per request
- Max 50,000 total input characters
- Max 10 unique speakers
- Max estimated audio duration: 600 seconds
- Total timeout: 600 seconds

**Response Headers (single_track):**

| Header                    | Description                                                              |
| ------------------------- | ------------------------------------------------------------------------ |
| `X-Audio-Duration-S`    | Total mixed audio duration                                               |
| `X-Synthesis-Latency-S` | Total synthesis latency                                                  |
| `X-Speakers-Unique`     | Number of unique speakers                                                |
| `X-Segment-Count`       | Number of synthesized segments                                           |
| `X-Skipped-Segments`    | Comma-separated list of skipped segment indices (if`on_error: "skip"`) |

---

## Voice Reference

### Presets

OpenAI-compatible preset names mapped to OmniVoice design prompts:

| Preset      | Gender | Age         | Pitch     | Accent     |
| ----------- | ------ | ----------- | --------- | ---------- |
| `alloy`   | female | young adult | moderate  | american   |
| `ash`     | male   | young adult | low       | american   |
| `ballad`  | male   | middle-aged | low       | british    |
| `cedar`   | male   | middle-aged | low       | american   |
| `coral`   | female | young adult | high      | australian |
| `echo`    | male   | middle-aged | moderate  | canadian   |
| `fable`   | female | middle-aged | moderate  | british    |
| `marin`   | female | middle-aged | moderate  | canadian   |
| `nova`    | female | young adult | high      | american   |
| `onyx`    | male   | middle-aged | very low  | british    |
| `sage`    | female | elderly     | low       | british    |
| `shimmer` | female | young adult | very high | american   |
| `verse`   | male   | young adult | moderate  | british    |

Usage: `"voice": "nova"` or `"voice": "openai:nova"`

---

### Design Attributes

Custom voices can be designed by combining attributes:

```json
{
  "instructions": "female,british accent,young adult,high pitch"
}
```

Or via the `voice` field:

```json
{
  "voice": "design:female,british accent,young adult,high pitch"
}
```

**Available attributes:**

| Category             | Options                                                                                                                                                                                                       |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **gender**     | `male`, `female`                                                                                                                                                                                          |
| **age**        | `child`, `teenager`, `young adult`, `middle-aged`, `elderly`                                                                                                                                        |
| **pitch**      | `very low pitch`, `low pitch`, `moderate pitch`, `high pitch`, `very high pitch`                                                                                                                    |
| **style**      | `whisper`                                                                                                                                                                                                   |
| **accent_en**  | `american accent`, `british accent`, `australian accent`, `chinese accent`, `canadian accent`, `indian accent`, `korean accent`, `portuguese accent`, `russian accent`, `japanese accent` |
| **dialect_zh** | `河南话`, `陕西话`, `四川话`, `贵州话`, `云南话`, `桂林话`, `济南话`, `石家庄话`, `甘肃话`, `宁夏话`, `青岛话`, `东北话`                                                              |

---

## Error Handling

All errors return JSON with an `error` or `detail` field:

| Status Code | Meaning                                           | Example                                                          |
| ----------- | ------------------------------------------------- | ---------------------------------------------------------------- |
| `400`     | Bad request (e.g. streaming with non-PCM format)  | `{"detail": "Streaming only supports response_format='pcm'"}`  |
| `404`     | Resource not found (profile, model)               | `{"detail": "Profile 'my_voice' not found"}`                   |
| `405`     | Method not allowed                                | `{"detail": "Method Not Allowed"}`                             |
| `409`     | Conflict (profile already exists)                 | `{"detail": "Profile 'my_voice' already exists"}`              |
| `413`     | Upload too large                                  | `{"detail": "Upload too large: 30.0MB exceeds limit of 25MB"}` |
| `422`     | Validation error                                  | `{"detail": "Unsupported voice value 'foo'..."}`               |
| `500`     | Internal server error                             | `{"detail": "Synthesis failed: ..."}`                          |
| `503`     | Service unavailable (model loading / at capacity) | `{"detail": "Script synthesis at capacity"}`                   |
| `504`     | Request timeout                                   | `{"detail": "Synthesis timed out after 120s"}`                 |

---

## Python Client Example

```python
import requests

BASE_URL = "http://localhost:8080"
API_KEY = None  # Set if auth is enabled
headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}

# Basic synthesis
response = requests.post(
    f"{BASE_URL}/v1/audio/speech",
    headers={**headers, "Content-Type": "application/json"},
    json={
        "model": "omnivoice",
        "input": "Hello from Python!",
        "voice": "nova",
        "response_format": "wav",
    },
)
with open("output.wav", "wb") as f:
    f.write(response.content)

# Voice cloning (one-shot)
with open("reference.wav", "rb") as ref:
    response = requests.post(
        f"{BASE_URL}/v1/audio/speech/clone",
        headers=headers,
        data={
            "text": "This is cloned speech.",
            "ref_text": "The reference text.",
            "speed": "1.0",
        },
        files={"ref_audio": ref},
    )
    with open("cloned.wav", "wb") as f:
        f.write(response.content)
```

---

## OpenAI SDK Compatibility

The server is designed as a drop-in replacement for the OpenAI TTS API:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="not-needed",  # Set if auth is enabled
)

response = client.audio.speech.create(
    model="tts-1",
    voice="nova",
    input="Hello from the OpenAI SDK!",
)
response.write_to_file("output.wav")
```

 #!/bin/bash

# Remote endpoint tests for omnivoice-server deployed on K8s.

# All requests go through the inference proxy with Bearer token auth.

# SSL verification is disabled (self-signed cert / internal CA).

# Usage: ./test_all_endpoints.sh

set -euo pipefail

# --- Configuration ---

BASE_URL="https://inference.adeoaiengine.ecouncil.ae/models/b55d006f-49c2-494d-9b8e-7575741c0e70/proxy"
API_KEY="sk-ah3DuYR8o0DvA8hhIsowmx4-YiWHImksQbb94ArFFJM"
AUTH_HEADER="Authorization: Bearer ${API_KEY}"
OUTPUT_DIR="/tmp/remote-tts-test"

# Reference audio for clone tests (local QA sample)

REF_AUDIO="/home/jyao/ADEO/tts/omnivoice/omnivoice-server/samples/qa/A01_default_no_params.wav"

# curl common flags: disable SSL verification, follow redirects, 60s timeout

CURL_OPTS="-sk --max-time 120 --connect-timeout 10"

# Track results

PASS=0
FAIL=0
FAILED_TESTS=()

mkdir -p "${OUTPUT_DIR}"

# --- Helpers ---

run_test() {
    local name="$1"
    echo ""
    echo "=== ${name} ==="
}

check_http() {
    local name="$1"
    local expected="$2"
    local actual="$3"
    if [ "${actual}" = "${expected}" ]; then
        echo "  ✅ HTTP ${actual}"
        PASS=$((PASS + 1))
    else
        echo "  ❌ HTTP ${actual} (expected ${expected})"
        FAIL=$((FAIL + 1))
        FAILED_TESTS+=("${name}")
    fi
}

# ============================================================

# 1. GET /health

# ============================================================

run_test "1. GET /health"
RESP=$(curl ${CURL_OPTS} -w "\n%{http_code}" -H "${AUTH_HEADER}" 
    "${BASE_URL}/health")
HTTP_CODE=$(echo "${RESP}" | tail -1)
BODY=$(echo "${RESP}" | sed '$d')
echo "  ${BODY}"
check_http "health" "200" "${HTTP_CODE}"

# ============================================================

# 2. GET /health-check

# ============================================================

run_test "2. GET /health-check"
RESP=$(curl ${CURL_OPTS} -w "\n%{http_code}" -H "${AUTH_HEADER}" 
    "${BASE_URL}/health-check")
HTTP_CODE=$(echo "${RESP}" | tail -1)
BODY=$(echo "${RESP}" | sed '$d')
echo "  ${BODY}"
check_http "health-check" "200" "${HTTP_CODE}"

# ============================================================

# 3. GET /v1/models

# ============================================================

run_test "3. GET /v1/models"
RESP=$(curl ${CURL_OPTS} -w "\n%{http_code}" -H "${AUTH_HEADER}" 
    "${BASE_URL}/v1/models")
HTTP_CODE=$(echo "${RESP}" | tail -1)
BODY=$(echo "${RESP}" | sed '$d')
echo "  ${BODY}" | python3 -m json.tool 2>/dev/null || echo "  ${BODY}"
check_http "models" "200" "${HTTP_CODE}"

# ============================================================

# 4. GET /v1/voices

# ============================================================

run_test "4. GET /v1/voices"
RESP=$(curl ${CURL_OPTS} -w "\n%{http_code}" -H "${AUTH_HEADER}" 
    "${BASE_URL}/v1/voices")
HTTP_CODE=$(echo "${RESP}" | tail -1)
BODY=$(echo "${RESP}" | sed '$d')
echo "  ${BODY}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'  voices: {d[\"total\"]} total')" 2>/dev/null || echo "  ${BODY}"
check_http "voices" "200" "${HTTP_CODE}"

# ============================================================

# 5. POST /v1/audio/speech (default voice)

# ============================================================

run_test "5. POST /v1/audio/speech (default)"
HTTP_CODE=$(curl ${CURL_OPTS} -o "${OUTPUT_DIR}/speech_default.wav" 
    -w "%{http_code}"
    -X POST "${BASE_URL}/v1/audio/speech"
    -H "${AUTH_HEADER}"
    -H "Content-Type: application/json"
    -d '{
        "model": "omnivoice",
        "input": "Hello, this is a test of the OmniVoice text-to-speech system.",
        "response_format": "wav"
    }')
check_http "speech-default" "200" "${HTTP_CODE}"
file "${OUTPUT_DIR}/speech_default.wav" 2>/dev/null && echo "  size:$(stat -c%s "${OUTPUT_DIR}/speech_default.wav" 2>/dev/null) bytes"

# ============================================================

# 6. POST /v1/audio/speech (design voice)

# ============================================================

run_test "6. POST /v1/audio/speech (design voice)"
HTTP_CODE=$(curl ${CURL_OPTS} -o "${OUTPUT_DIR}/speech_design.wav" 
    -w "%{http_code}"
    -X POST "${BASE_URL}/v1/audio/speech"
    -H "${AUTH_HEADER}"
    -H "Content-Type: application/json"
    -d '{
        "model": "omnivoice",
        "input": "This voice has been designed with specific attributes.",
        "instructions": "female,british accent,young adult,high pitch",
        "response_format": "wav"
    }')
check_http "speech-design" "200" "${HTTP_CODE}"
file "${OUTPUT_DIR}/speech_design.wav" 2>/dev/null && echo "  size:$(stat -c%s "${OUTPUT_DIR}/speech_design.wav" 2>/dev/null) bytes"

# ============================================================

# 7. POST /v1/audio/speech (preset: nova)

# ============================================================

run_test "7. POST /v1/audio/speech (preset nova)"
HTTP_CODE=$(curl ${CURL_OPTS} -o "${OUTPUT_DIR}/speech_nova.wav" 
    -w "%{http_code}"
    -X POST "${BASE_URL}/v1/audio/speech"
    -H "${AUTH_HEADER}"
    -H "Content-Type: application/json"
    -d '{
        "model": "omnivoice",
        "input": "Testing the nova preset voice.",
        "voice": "nova",
        "response_format": "wav"
    }')
check_http "speech-nova" "200" "${HTTP_CODE}"
file "${OUTPUT_DIR}/speech_nova.wav" 2>/dev/null && echo "  size:$(stat -c%s "${OUTPUT_DIR}/speech_nova.wav" 2>/dev/null) bytes"

# ============================================================

# 8. POST /v1/audio/speech (streaming PCM)

# ============================================================

run_test "8. POST /v1/audio/speech (streaming PCM)"
HTTP_CODE=$(curl ${CURL_OPTS} -o "${OUTPUT_DIR}/speech_stream.pcm" 
    -w "%{http_code}"
    -X POST "${BASE_URL}/v1/audio/speech"
    -H "${AUTH_HEADER}"
    -H "Content-Type: application/json"
    -d '{
        "model": "omnivoice",
        "input": "This is a longer text that will be streamed in chunks. Each sentence is synthesized and sent as soon as it is ready.",
        "stream": true,
        "response_format": "pcm",
        "position_temperature": 0.0
    }')
check_http "speech-stream" "200" "${HTTP_CODE}"
file "${OUTPUT_DIR}/speech_stream.pcm" 2>/dev/null && echo "  size:$(stat -c%s "${OUTPUT_DIR}/speech_stream.pcm" 2>/dev/null) bytes"

# ============================================================

# 9. POST /v1/audio/speech/clone (one-shot, WITH ref_text)

# ============================================================

run_test "9. POST /v1/audio/speech/clone (one-shot, with ref_text)"
HTTP_CODE=$(curl ${CURL_OPTS} -o "${OUTPUT_DIR}/clone_oneshot.wav" 
    -w "%{http_code}"
    -X POST "${BASE_URL}/v1/audio/speech/clone"
    -H "${AUTH_HEADER}"
    -F "text=This is one-shot voice cloning without saving a profile."
    -F "ref_audio=@${REF_AUDIO}"
    -F "ref_text=Hello, this is a test of the OmniVoice text-to-speech system."
    -F "speed=1.0")
check_http "clone-oneshot" "200" "${HTTP_CODE}"
file "${OUTPUT_DIR}/clone_oneshot.wav" 2>/dev/null && echo "  size:$(stat -c%s "${OUTPUT_DIR}/clone_oneshot.wav" 2>/dev/null) bytes"

# ============================================================

# 10. POST /v1/audio/speech/clone (one-shot, WITHOUT ref_text → Whisper ASR)

# ============================================================

run_test "10. POST /v1/audio/speech/clone (no ref_text → Whisper ASR)"
HTTP_CODE=$(curl ${CURL_OPTS} -o "${OUTPUT_DIR}/clone_no_reftext.wav" 
    -w "%{http_code}"
    -X POST "${BASE_URL}/v1/audio/speech/clone"
    -H "${AUTH_HEADER}"
    -F "text=This is voice cloning without providing reference text, triggering Whisper ASR."
    -F "ref_audio=@${REF_AUDIO}"
    -F "speed=1.0")
check_http "clone-no-reftext" "200" "${HTTP_CODE}"
file "${OUTPUT_DIR}/clone_no_reftext.wav" 2>/dev/null && echo "  size:$(stat -c%s "${OUTPUT_DIR}/clone_no_reftext.wav" 2>/dev/null) bytes"

# ============================================================

# 11. POST /v1/voices/profiles (create)

# ============================================================

run_test "11. POST /v1/voices/profiles (create)"
RESP=$(curl ${CURL_OPTS} -w "\n%{http_code}" 
    -X POST "${BASE_URL}/v1/voices/profiles"
    -H "${AUTH_HEADER}"
    -F "profile_id=my_voice"
    -F "ref_audio=@${REF_AUDIO}"
    -F "ref_text=Hello, this is a test of the OmniVoice text-to-speech system."
    -F "overwrite=true")
HTTP_CODE=$(echo "${RESP}" | tail -1)
BODY=$(echo "${RESP}" | sed '$d')
echo "  ${BODY}" | python3 -m json.tool 2>/dev/null || echo "  ${BODY}"
check_http "profile-create" "201" "${HTTP_CODE}"

# ============================================================

# 12. GET /v1/voices/profiles/my_voice

# ============================================================

run_test "12. GET /v1/voices/profiles/my_voice"
RESP=$(curl ${CURL_OPTS} -w "\n%{http_code}" 
    -H "${AUTH_HEADER}"
    "${BASE_URL}/v1/voices/profiles/my_voice")
HTTP_CODE=$(echo "${RESP}" | tail -1)
BODY=$(echo "${RESP}" | sed '$d')
echo "  ${BODY}" | python3 -m json.tool 2>/dev/null || echo "  ${BODY}"
check_http "profile-get" "200" "${HTTP_CODE}"

# ============================================================

# 13. PATCH /v1/voices/profiles/my_voice (update ref_text)

# ============================================================

run_test "13. PATCH /v1/voices/profiles/my_voice (update)"
RESP=$(curl ${CURL_OPTS} -w "\n%{http_code}" 
    -X PATCH "${BASE_URL}/v1/voices/profiles/my_voice"
    -H "${AUTH_HEADER}"
    -F "ref_text=Updated reference text for the voice profile.")
HTTP_CODE=$(echo "${RESP}" | tail -1)
BODY=$(echo "${RESP}" | sed '$d')
echo "  ${BODY}" | python3 -m json.tool 2>/dev/null || echo "  ${BODY}"
check_http "profile-update" "200" "${HTTP_CODE}"

# ============================================================

# 14. POST /v1/audio/speech (clone:my_voice profile)

# ============================================================

run_test "14. POST /v1/audio/speech (clone:profile)"
HTTP_CODE=$(curl ${CURL_OPTS} -o "${OUTPUT_DIR}/clone_profile.wav" 
    -w "%{http_code}"
    -X POST "${BASE_URL}/v1/audio/speech"
    -H "${AUTH_HEADER}"
    -H "Content-Type: application/json"
    -d '{
        "model": "omnivoice",
        "input": "This uses a stored clone profile for voice cloning.",
        "voice": "clone:my_voice",
        "response_format": "wav"
    }')
check_http "clone-profile" "200" "${HTTP_CODE}"
file "${OUTPUT_DIR}/clone_profile.wav" 2>/dev/null && echo "  size:$(stat -c%s "${OUTPUT_DIR}/clone_profile.wav" 2>/dev/null) bytes"

# ============================================================

# 15. DELETE /v1/voices/profiles/my_voice

# ============================================================

run_test "15. DELETE /v1/voices/profiles/my_voice"
HTTP_CODE=$(curl ${CURL_OPTS} -o /dev/null -w "%{http_code}" 
    -X DELETE "${BASE_URL}/v1/voices/profiles/my_voice"
    -H "${AUTH_HEADER}")
check_http "profile-delete" "204" "${HTTP_CODE}"

# ============================================================

# 16. GET /v1/voices/profiles/my_voice (after delete → 404)

# ============================================================

run_test "16. GET /v1/voices/profiles/my_voice (after delete)"
RESP=$(curl ${CURL_OPTS} -w "\n%{http_code}" 
    -H "${AUTH_HEADER}"
    "${BASE_URL}/v1/voices/profiles/my_voice")
HTTP_CODE=$(echo "${RESP}" | tail -1)
BODY=$(echo "${RESP}" | sed '$d')
echo "  ${BODY}"
check_http "profile-after-delete" "404" "${HTTP_CODE}"

# ============================================================

# 17. POST /v1/audio/script (single_track)

# ============================================================

run_test "17. POST /v1/audio/script (single_track)"
HTTP_CODE=$(curl ${CURL_OPTS} -o "${OUTPUT_DIR}/script_single.wav" 
    -w "%{http_code}"
    -X POST "${BASE_URL}/v1/audio/script"
    -H "${AUTH_HEADER}"
    -H "Content-Type: application/json"
    -d '{
        "script": [
            {"speaker": "narrator", "text": "Welcome to the OmniVoice script synthesis demo.", "voice": "openai:onyx"},
            {"speaker": "alice", "text": "Hello! I am Alice, and I will be your guide today.", "voice": "openai:nova"},
            {"speaker": "narrator", "text": "Thank you Alice. Let us begin.", "voice": "openai:onyx"}
        ],
        "default_voice": "openai:onyx",
        "speed": 1.0,
        "on_error": "abort",
        "pause_between_speakers": 0.3,
        "response_format": "wav",
        "output_format": "single_track"
    }')
check_http "script-single" "200" "${HTTP_CODE}"
file "${OUTPUT_DIR}/script_single.wav" 2>/dev/null && echo "  size:$(stat -c%s "${OUTPUT_DIR}/script_single.wav" 2>/dev/null) bytes"

# ============================================================

# 18. POST /v1/audio/script (multi_track)

# ============================================================

run_test "18. POST /v1/audio/script (multi_track)"
HTTP_CODE=$(curl ${CURL_OPTS} -o "${OUTPUT_DIR}/script_multi.json" 
    -w "%{http_code}"
    -X POST "${BASE_URL}/v1/audio/script"
    -H "${AUTH_HEADER}"
    -H "Content-Type: application/json"
    -d '{
        "script": [
            {"speaker": "narrator", "text": "Welcome to the OmniVoice script synthesis demo.", "voice": "openai:onyx"},
            {"speaker": "alice", "text": "Hello! I am Alice, and I will be your guide today.", "voice": "openai:nova"}
        ],
        "default_voice": "openai:onyx",
        "speed": 1.0,
        "on_error": "abort",
        "pause_between_speakers": 0.3,
        "response_format": "wav",
        "output_format": "multi_track"
    }')
check_http "script-multi" "200" "${HTTP_CODE}"
echo "  Speakers:"$(python3 -c "import json; d=json.load(open('${OUTPUT_DIR}/script_multi.json')); print(list(d.get('tracks',{}).keys()))" 2>/dev/null || echo "(parse failed)")

# ============================================================

# Summary

# ============================================================

echo ""
echo "============================================"
echo "  RESULTS: ${PASS} passed, ${FAIL} failed"
echo "============================================"
if [ ${FAIL} -gt 0 ]; then
    echo ""
    echo "Failed tests:"
    for t in "${FAILED_TESTS[@]}"; do
        echo "  ❌ ${t}"
    done
    exit 1
fi
echo "All tests passed! ✅"
