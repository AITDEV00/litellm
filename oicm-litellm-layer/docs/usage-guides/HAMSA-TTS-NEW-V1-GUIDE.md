# Hamsa TTS New: `/v1` Endpoint Guide

> **Scope**: How to call `hamsa-tts-new` text-to-speech through the LiteLLM gateway, with sample curl commands and expected response shapes
>
> **Related docs**: [HAMSA_STT_TTS_GUIDE.md](HAMSA_STT_TTS_GUIDE.md) (original `hamsa-tts`, voice management details), [GATEWAY_GUIDE.md](GATEWAY_GUIDE.md) (gateway overview)

**Gateway URL**: `https://litellm.ecouncil.ae` (self-signed TLS → always use `curl -k`)

**API Key**: replace `<your-api-key>` everywhere below with your LiteLLM gateway API key. All requests use the `Authorization: Bearer <your-api-key>` header.

**Model**: `hamsa-tts-new`

**Verified**: 2026-09-09

---

## Gateway endpoints

The gateway supports two `/v1` endpoints for this model:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/models` | GET | List available models |
| `/v1/audio/speech` | POST | Synthesize speech (WAV) |

No other `/v1` endpoints (voices, voice-clone, status, ...) are exposed on the gateway.

---

## List models

```bash
curl -sk "https://litellm.ecouncil.ae/v1/models" \
  -H "Authorization: Bearer <your-api-key>"
```

Response (trimmed):

```json
{
  "object": "list",
  "data": [
    { "id": "hamsa-tts-new", "object": "model", "owned_by": "ADEO" }
  ]
}
```

---

## Synthesize speech (TTS)

### Endpoint

```
POST /v1/audio/speech
```

### Request shape

Returns **WAV audio** (16-bit mono 16 kHz PCM). Save the binary response to a file because the response body is raw audio, not JSON.

| Field | Required | Default | Notes |
|-------|----------|---------|-------|
| `model` | yes | | Must be `hamsa-tts-new` |
| `input` | yes | | Text to synthesize (Arabic or English) |
| `voice` | yes | | Speaker name — see voice list below |
| `response_format` | no | `wav` | `wav` |
| `speed` | no | `1.0` | `0.5` – `2.0` |
| `expressiveness` | no | `1.0` | `0.0` – `2.0` |

### Sample curl

```bash
curl -sk -X POST "https://litellm.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "hamsa-tts-new",
    "input": "مرحبا بكم في مجلس أبوظبي",
    "voice": "Zeina",
    "response_format": "wav"
  }' \
  -o speech.wav
```

### Response

Binary audio with `Content-Type: audio/wav`. Verified result:

```text
HTTP 200 — speech.wav: RIFF (little-endian) data, WAVE audio,
Microsoft PCM, 16 bit, mono 16000 Hz
```

> **Note**: the WAV header contains `0xFFFFFFFF` in the RIFF/data size fields
> (streaming-style header from the backend). `ffprobe`, `file` and audio
> players read it fine, but Python's built-in `wave` module misreports the
> duration. If you need exact size, trust the file length, not the header.

### More examples

Different voice:

```bash
curl -sk -X POST "https://litellm.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"hamsa-tts-new","input":"السلام عليكم","voice":"Amir","response_format":"wav"}' \
  -o amir.wav
```

Faster and more expressive:

```bash
curl -sk -X POST "https://litellm.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"hamsa-tts-new","input":"أهلا وسهلا","voice":"Layla","speed":1.3,"expressiveness":1.5,"response_format":"wav"}' \
  -o layla.wav
```

---

## Voices

### Cloned customer voices (ADEO) — 12 voices

Cloned from `Voices&transcription.xlsx` on 2026-09-09. All verified live on
`hamsa-tts-new` (HTTP 200 → WAV). Pass the **`adeo_`-prefixed** name in `voice`
— these are not the bare names, because several built-ins share the same name
(`Mariam`, `Reem`, `Faisal`, `Leo`, `Emma`, `Lily`, `Claire`, `Henry`, `Mia`).

| Display | Speaker (`voice` value) | Language / Dialect |
|---|---|---|
| Mariam | `adeo_mariam` | Arabic (Egyptian) |
| Omar | `adeo_omar` | Arabic (Egyptian) |
| Karim | `adeo_karim` | Arabic (Levantine) |
| Lara | `adeo_lara` | Arabic (Levantine) |
| Reem | `adeo_reem` | Arabic (Saudi) |
| Faisal | `adeo_faisal` | Arabic (Saudi) |
| Leo | `adeo_leo` | English |
| Emma | `adeo_emma` | English |
| Lily | `adeo_lily` | English |
| Claire | `adeo_claire` | English |
| Henry | `adeo_henry` | English |
| Mia | `adeo_mia` | English |

```bash
curl -sk -X POST "https://litellm.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"hamsa-tts-new","input":"مرحبا بك في مجلس أبوظبي","voice":"adeo_mariam","response_format":"wav"}' \
  -o mariam_clone.wav
```

> Clones are registered as `adeo_<name>` and served through this model. The
> reference audio lives in pod `/tmp` (ephemeral) — if the pod restarts, re-run
> the clone flow to re-register the voices.

### Built-in speakers

**113 bundled speakers** (Arabic + English), passed as the `voice` field.

Commonly used: `Zeina`, `Amir`, `Layla`, `Yara`, `Salma`, `Tamer`, `Noor`, `Sami`, `Hiba`, `Fahd`

Full list (113 names, comma separated):

ASSY, AbdelQader, Ahmed, Akmal, Ali, Alia, Amanda, Amir, Amira, Amjad, Aml, Arjun, Ayman, Barbara, Brian, Carla, Dalal, David, Dima, Edward, Eman, Eyad, Fady, Fahd, Faiza, Fares, Fatma, Fouad, Gannat, Gassan, Ghazal, Hady, Hafsa, Hamdan, Haneen, Hasan, Hatem, Hiba, Hind, Jaber, Jana, Jasem, John, Kamla, Khadiga, Khadija, Lana, Layan, Layla, Lyali, Magda, Maha, Maher, Mai, Mais, Majd, Majid, Mansour, Mariam, Marwa, Marwan, Mazen, Michael, Nabil, Nada, Nadya, Nagib, Nermin, Noah, Noor, Noura, Nouran, Obida, Ola, Othman, Raghad, Rami, Rania, Razan, Reem, Rema, Renat, Rihanna, Robert, Roger, Ruba, Safa, Salam, Saleh, Salem, Salim, Salma, Salwa, Saly, Samer, Sami, Samir, Sandra, Sarah, Sawsan, Sayed, Shaker, Somaya, Souad, Suzan, Talin, Tamer, Tasneem, Wael, William, Yara, Yehya, Zeina

---

## Errors

| Case | HTTP | Body |
|------|------|------|
| Unknown `voice` | 400 | `speaker_not_found: speaker 'X' not loaded` |
| Missing/wrong key | 401 | `auth_error` |
| Key without model access | 403 | `key_model_access_denied` |
| Empty `input` | 400/422 | validation error |

---

## OpenAI SDK (Python)

Verified live with `openai` SDK 3.9.0 against the gateway:

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://litellm.ecouncil.ae/v1",
    api_key="<your-api-key>",
)

with client.audio.speech.with_streaming_response.create(
    model="hamsa-tts-new",
    voice="Zeina",
    input="أهلا وسهلا",
) as response:
    response.stream_to_file("speech.wav")
```

Verified result:

```text
client.models.list()   -> ['hamsa-tts-new']
speech.wav             -> RIFF WAVE, Microsoft PCM, ~31 KB, 0.98 s (ffprobe)
```
