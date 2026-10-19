# Pyannote Speaker Diarization: Curl Guide

> **Scope**: How to call pyannote speaker diarization (`pyannote/community-1`) through the LiteLLM gateway's `/v1/diarize` route, with sample curl commands, expected response shapes, error contracts, and verified performance numbers
>
> **Related docs**: [VIBEVOICE_ASR_GUIDE.md](VIBEVOICE_ASR_GUIDE.md) (chat-style ASR), [HAMSA_STT_TTS_GUIDE.md](HAMSA_STT_TTS_GUIDE.md) (native multipart STT), [../custom-providers/PYANNOTE_DIARIZATION_PROVIDER_PLAN.md](../custom-providers/PYANNOTE_DIARIZATION_PROVIDER_PLAN.md) (implementation plan)

---

## Gateway endpoint

| Capability | Gateway URL |
| ---------- | ----------- |
| Speaker diarization | `https://litellm.ecouncil.ae/v1/diarize` |

Authentication is the standard LiteLLM gateway key passed as a Bearer token. The coro pod itself does not require auth, but the gateway always requires it:

```
Authorization: Bearer <your-api-key>
```

The gateway maps the user-facing model name to the internal deployment. The discovery controller registers the model as `pyannote/community-1` (mode `diarization`), and the coro pod accepts and expects exactly that id. Send `model=pyannote/community-1`; omit it only if exactly one diarization deployment is registered.

> **Important**: this is the pyannoteAI `/v1/diarize` contract (multipart audio file + form fields), not OpenAI `/v1/audio/transcriptions`. There is no base64, no JSON body, and no chat surface on this model.

---

## Model overview

| Property | Value |
| -------- | ----- |
| Model ID | `pyannote/community-1` |
| Underlying engine | coro `speakrs` backend (`cuda-fast`, 4 workers) on a MIG-2g.35gb GPU slice |
| Type | Audio speaker diarization (who spoke when) |
| Mode | `diarization` |
| Input | Multipart WAV/FLAC/OGG/MP3 file upload |
| Output | pyannoteAI `DiarizationJob` JSON, inline synchronous `succeeded` job |
| Provider | `pyannote` |

Confirm it is registered on the gateway:

```bash
curl -s "https://litellm.ecouncil.ae/v1/models" \
  -H "Authorization: Bearer <your-api-key>"
```

---

## 1. Diarize audio (single request)

### Endpoint

```
POST /v1/diarize
```

### Request shape

Multipart form data, pyannoteAI `DiarizeRequest` field names. Supported fields:

| Field | Required | Notes |
| ----- | -------- | ----- |
| `file` | yes | The audio file part (WAV/FLAC/OGG/MP3) |
| `model` | no* | Must be `pyannote/community-1` |
| `exclusive` | no | `true` -> also fill `output.exclusiveDiarization` (non-overlapping turns only) |
| `webhook` / `webhookStatusOnly` | no | Accepted but jobs are synchronous; ignored in practice |
| `vadSensitivity` / `crosstalkSensitivity` | no | Passed through to the engine |

\* omitting `model` works only when the gateway has exactly one diarization deployment registered; the gateway then defaults it. Unknown spec fields are silently dropped by the gateway route (they never reach the pod), so check field names carefully.

Fields that look supported but are rejected by the engine with a 400 (spec-shaped `ValidationErrorResponse` fragment) include `numSpeakers`, `minSpeakers`, `maxSpeakers`, `confidence`, `turnLevelConfidence`, `transcription`, `transcriptionConfig`, `speechProbability`, `crosstalkProbability`, and `speakerProbability`. The engine detects speaker count itself.

### Sample curl

```bash
curl -s "https://litellm.ecouncil.ae/v1/diarize" \
  -H "Authorization: Bearer <your-api-key>" \
  -F "model=pyannote/community-1" \
  -F "exclusive=true" \
  -F "file=@meeting.wav"
```

### Response shape

A pyannoteAI `DiarizationJob`, returned inline and already `succeeded` (coro runs jobs synchronously; `createdAt` == `updatedAt`):

```json
{
  "jobId": "03d3d5f95d6d491b8c9606445d17d16c",
  "status": "succeeded",
  "createdAt": "2026-10-19T18:52:37Z",
  "updatedAt": "2026-10-19T18:52:37Z",
  "output": {
    "diarization": [
      {"speaker": "SPEAKER_00", "start": 0.31784375, "end": 2.22471875},
      {"speaker": "SPEAKER_00", "start": 3.28784375, "end": 4.38471875},
      {"speaker": "SPEAKER_01", "start": 8.14784375, "end": 10.51034375}
    ],
    "exclusiveDiarization": ["... same shape, non-overlapping turns ..."]
  }
}
```

| Response key | Type | Meaning |
| ------------ | ---- | ------- |
| `jobId` | string | Job identifier (last 128 jobs queryable on the pod) |
| `status` | string | `succeeded` on success |
| `output.diarization` | array | Speaker turns, overlaps preserved |
| `output.exclusiveDiarization` | array | Present when `exclusive=true`: non-overlapping turns only |
| `output.warning` | string | Pod-side warnings (e.g. clipping detection) |

Every segment is `{"speaker": "SPEAKER_XX", "start": <sec>, "end": <sec>}` with `end > start`. Speaker labels are stable within a job; no metadata about the speakers is provided beyond ordinal labels.

---

## 2. Error contracts (verified live)

| Case | HTTP | Body |
| ---- | ---- | ---- |
| Missing/invalid gateway key | 401 | LiteLLM auth error |
| Well-typed but unsupported field (`numSpeakers=2`) | 400 | `litellm.BadRequestError: PyannoteException` wrapping the pod's spec-shaped `{"detail":[{"field":"numSpeakers","message":"..."}]}` |
| Wrong-typed field (`numSpeakers=not-a-number`) | 422 | Pod's FastAPI `int_parsing` validation `{"detail":[...]}` |
| Unknown field (`bogus=1`) | 200 | Silently dropped by the gateway route; request proceeds |

```bash
# per-field 400 example: engine does not accept numSpeakers
curl -s "https://litellm.ecouncil.ae/v1/diarize" \
  -H "Authorization: Bearer <your-api-key>" \
  -F "file=@meeting.wav" \
  -F "numSpeakers=2"
# -> HTTP 400, body carries the pod's per-field validation message verbatim
```

The gateway never invents validation errors for this route: 400/422 bodies are the pod's own error responses, wrapped by the gateway error handler. If you need per-field detail, read the `PyannoteException` payload inside `error.message`.

---

## 3. Performance (stress-tested 2026-10-19, dev gateway)

Verified with `oicm-litellm-layer/scripts/stress_diarize.py` (stdlib-only harness, phases: gate -> contract -> single -> concurrency -> long clip), against the dev gateway port-forward with the coro pod on `adeo-gpu-01` (4 CPU / 16 Gi / MIG-2g.35gb).

| Scenario | Result |
| -------- | ------ |
| Single 60s clip | 1.0-1.6s wall, 37-58x RTFx, 2 speakers detected |
| 8 clients x 5 rounds (40 reqs, 60s clips) | 0 errors, 40/40 succeeded, agg 58-79x RTFx |
| Concurrent latency (40 reqs) | p50 4.7-7.6s, p95 9.7-10.5s, p99 10.2-12.8s |
| 17.5 min AMI meeting (ES2004a, 1049s) | 19.6-29.9s wall, 35-54x RTFx, 5 speakers, 232 segments |
| Pod temp hygiene | 0 leftover upload temp files in pod `/tmp` after 42 requests |

Key facts:

- Jobs are synchronous through the gateway: the HTTP response IS the completed job. There is no polling step.
- Throughput scales with the pod's diarization workers (4 workers, `cuda-fast`); aggregate concurrency beyond worker count queues rather than errors.
- `RTFx` above = audio seconds / wall seconds. Single-stream on the 4090-class slice historically reaches ~2000x+ for the diarization backend alone; end-to-end through the gateway includes HTTP + multipart overhead.

Reproduce:

```bash
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14000:4000   # dev
GATEWAY_KEY=$(python3 oicm-litellm-layer/scripts/get_master_key.py) \
DIARIZE_CONCURRENCY=8 DIARIZE_ROUNDS=5 \
  python3 oicm-litellm-layer/scripts/stress_diarize.py
```

---

## 4. Route/field notes for clients

- The multipart file part must be named `file` (pyannoteAI contract).
- `model` omission relies on the gateway resolving the only registered diarization deployment; explicitly sending `model=pyannote/community-1` is the stable contract.
- `exclusive=true` fills `output.exclusiveDiarization`; without it that key is omitted (or null).
- The pod keeps the last 128 job ids in memory for `GET /v1/jobs/{jobId}` on the pod's own port; the gateway does not expose a job-status route (jobs are synchronous, so it is not needed).
- Spend logs: this route (like all audio-slice routes) does not write LiteLLM spend rows; usage tracking is not available for diarization yet.
