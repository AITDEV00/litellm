# Pyannote Speaker Diarization: Curl Guide

> **Scope**: How to call pyannote speaker diarization (`pyannote/community-1`) through the LiteLLM gateway's `/v1/diarize` route, with sample curl commands, expected response shapes, error contracts, and verified performance numbers
>
> **Related docs**: [VIBEVOICE_ASR_GUIDE.md](VIBEVOICE_ASR_GUIDE.md) (chat-style ASR), [HAMSA_STT_GUIDE.md](HAMSA_STT_GUIDE.md) (native multipart STT), [../custom-providers/PYANNOTE_DIARIZATION_PROVIDER_PLAN.md](../custom-providers/PYANNOTE_DIARIZATION_PROVIDER_PLAN.md) (implementation plan)

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

### Which keys can call it, including `embeddings=true`

Route access is decided per route, not per form field. `/v1/diarize` is listed under `LiteLLMRoutes.litellm_native_routes`, which is part of `llm_api_routes`, so a key needs `/v1/diarize` inside the route groups it is allowed to call. `embeddings=true` adds no authorization requirement of its own: any key that can diarize can also request embeddings.

| Key | Calls `/v1/diarize` (with or without `embeddings`) |
| --- | --- |
| `key_type=llm_api` virtual key | Yes. Defaults to `allowed_routes=["llm_api_routes"]` |
| Any key whose `allowed_routes` includes `llm_api_routes`, `litellm_native_routes`, or the literal `"/v1/diarize"` | Yes |
| `key_type=read_only` (`allowed_routes=["info_routes"]`) | No, 403 |
| `key_type=management` (no `llm_api_routes`) | No, 403 |
| No key / bad key | No, 401 |

The model gate is separate: the key must also resolve `pyannote/community-1` as a callable model (via its own `models` list, its team's models, or `all-proxy-models`), otherwise the request is rejected before reaching the pod.

Verified live on `https://litellm.ecouncil.ae` with a throwaway `key_type=llm_api` key scoped to `models=["pyannote/community-1"]`: `embeddings=true` returned HTTP 200 with `speakerEmbeddings` for all three detected speakers and the full `embeddingModel` block. The same key with the flag absent returned only `diarization` + `exclusiveDiarization`, and with `embeddings=false` returned only `diarization`. A `read_only` key was refused with 403 and an unauthenticated call with 401.

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
| `embeddings` | no | `true` -> also fill `output.speakerEmbeddings` + `output.embeddingModel` (see §1.1) — a coro extension, NOT part of the pyannoteAI spec |

\* omitting `model` works only when the gateway has exactly one diarization deployment registered; the gateway then defaults it. Unknown form fields are ignored, not rejected: the pod's FastAPI handler declares only the fields above, so anything else never reaches the engine and cannot change the result. Check field names carefully, because a typo behaves exactly like an omitted field (`embeddingss=true` silently returns no vectors).

Invalid values for a known field ARE rejected, by the pod, with a 422 surfaced through the gateway as `litellm.BadRequestError: PyannoteException` wrapping the pod's Pydantic detail, e.g. `embeddings=maybe` -> `bool_parsing` on `body.embeddings`.

All other pyannoteAI request fields are REJECTED by the pod with a 400 spec-shaped `ValidationErrorResponse` (never silently honored): `numSpeakers`, `minSpeakers`, `maxSpeakers`, `confidence`, `turnLevelConfidence`, `transcription`, `transcriptionConfig`, `vadSensitivity`, `crosstalkSensitivity`, `speechProbability`, `crosstalkProbability`, `speakerProbability`, `webhook`, `webhookStatusOnly`. The engine detects the speaker count itself (threshold-based clustering) and jobs are synchronous, so webhooks cannot fire.

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
| `output.speakerEmbeddings` | object | Present when `embeddings=true`: label -> 256-float vector (see §1.1) |
| `output.embeddingModel` | object | Present when `embeddings=true`: `{id, revision, dim}` of the weights that produced the vectors |
| `output.warning` | string | Pod-side warnings (e.g. clipping detection) |

Every segment is `{"speaker": "SPEAKER_XX", "start": <sec>, "end": <sec>}` with `end > start`. Speaker labels are stable within a job; no metadata about the speakers is provided beyond ordinal labels.

### 1.1 Per-speaker embeddings (`embeddings=true`) — voice-ID flow

Add `-F embeddings=true` and the output gains two fields:

```bash
curl -s "https://litellm.ecouncil.ae/v1/diarize" \
  -H "Authorization: Bearer <your-api-key>" \
  -F "model=pyannote/community-1" \
  -F "embeddings=true" \
  -F "exclusive=true" \
  -F "file=@meeting.wav"
```

```json
"output": {
  "diarization": [ ... ],
  "exclusiveDiarization": [ ... ],
  "speakerEmbeddings": {
    "SPEAKER_00": [0.0312, -0.1147, "... 256 floats total ..."],
    "SPEAKER_01": [0.0498, 0.0733, "... 256 floats total ..."],
    "SPEAKER_02": null
  },
  "embeddingModel": {
    "id": "pyannote/speaker-diarization-community-1",
    "revision": "67f963b6a1d94fcd4678948237b3cc8bd69bf1d8",
    "dim": 256
  }
}
```

Contract rules (pinned by the pod's conformance suite):

- **Keys are exactly the speaker labels** that appear in the same job's
  `diarization`/`exclusiveDiarization` — one vector per diarized speaker.
- **Each value is 256 floats** — the posterior-weighted centroid of that
  speaker's WeSpeaker (ResNet34) embeddings across the recording — or
  `null` when the speaker is a padded row with no actual speech (rare;
  happens when the estimator's speaker count exceeds the kept clusters).
- **`embeddingModel.revision` is the pinned weights commit** — vectors are
  only comparable within the same revision.
- **Without the flag the keys are absent entirely** (not `null`), so the
  default response stays byte-identical to the official pyannoteAI
  contract and the official `pyannoteai-sdk` client keeps working
  unchanged. The flag is a coro extension and must never collide with a
  future upstream field (pinned by test).

Voice-ID usage notes:

- The vectors are **not L2-normalized**; normalize before cosine
  similarity, or use dot products on your own normalized copies.
- They are **stable for a given `revision`**: the same audio + weights
  produce the same vectors (deterministic inference), so enrolled
  voice IDs remain valid across requests.
- **A speakrs upgrade changes `revision`**: vectors from different
  revisions are NOT comparable — re-enrol stored voice IDs after any
  upgrade that changes the returned `revision`.

Checking a response from the shell:

```bash
curl -s "https://litellm.ecouncil.ae/v1/diarize" \
  -H "Authorization: Bearer $KEY" \
  -F "model=pyannote/community-1" \
  -F "embeddings=true" \
  -F "file=@meeting.wav" \
| jq '{
    status,
    speakers: [.output.diarization[].speaker] | unique,
    vectors: (.output.speakerEmbeddings | to_entries | map({(.key): (.value | length)})) | add,
    model: .output.embeddingModel
  }'
```

`vectors` should show one entry per speaker, each `256` (or `null` for a
padded row), and `model.revision` should be the pinned commit above.

Note on how the flag reaches the pod: the gateway route forwards every
multipart field except `file` and `model` verbatim, so `embeddings` needs
no gateway-side allowlist entry. Only the small set of spec fields listed
in §1 is read by the gateway itself for logging and model resolution.

---

## 2. Error contracts (verified live)

| Case | HTTP | Body |
| ---- | ---- | ---- |
| Missing/invalid gateway key | 401 | LiteLLM auth error |
| Key without `/v1/diarize` in its `allowed_routes` (e.g. `read_only`) | 403 | `Virtual key is not allowed to call this route. Only allowed to call routes: ['info_routes']. Tried to call route: /v1/diarize` |
| Well-typed but unsupported field (`numSpeakers=2`) | 400 | `litellm.BadRequestError: PyannoteException` wrapping the pod's spec-shaped `{"message":"Invalid request","errors":[{"field":"numSpeakers","message":"..."}]}` |
| Wrong-typed field (`embeddings=maybe`) | 422 | The pod's FastAPI validation detail (`bool_parsing`), wrapped by the gateway error handler |
| Unknown field (`zzz=1`) | 200 | Ignored by the pod's handler; the request proceeds unchanged |

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

## 3. Performance (benchmark evidence 2026-10-19, H200 MIG-2g.35gb slice)

Two measurement contexts matter and are kept separate:

1. **Engine RTFx (no network overhead)** — measured in-cluster on loopback (`127.0.0.1:8080` inside the coro pod), so the multipart upload runs at memory speed. This is the engine's true processing speed and the number for capacity planning.
2. **Through-gateway RTFx** — measured from outside via the gateway; includes client->gateway->pod transfer of the audio bytes plus HTTP overhead. Large-file uploads dominate this number on slow transports.

### 3.1 Engine-only RTF (network excluded) — the diarization engine's real speed

Measured with the coro repo's `tests/local/bench_diarize_k8s.py` (phase-decomposed: upload / server / download), executed **inside the pod** against `http://127.0.0.1:8080`; audio = 60-minute 16 kHz speech clip (110 MiB, 1440 segments), `model=pyannote/community-1`, `exclusive=true`:

| Load (in-flight) | Upload (loopback) | Server time / req | **Engine RTFx (audio/server)** | e2e RTFx (audio/total) |
| --------------- | ----------------- | ----------------- | ------------------------------ | ---------------------- |
| 1 stream | 0.28 s | 4.95 s | **727x** | 688x |
| 4 concurrent | ~1.1 s | ~13.9 s | 259x mean per req | **928x aggregate** |
| 8 concurrent | ~2.1 s | ~21.8 s | 165x mean per req | **969x aggregate** |
| 16 concurrent | ~7.3 s | ~40.9 s | 88x mean per req | 899x aggregate (plateau) |

- **RTFx = audio-seconds processed per wall-second; RTF = 1/RTFx.**
- **Single request: RTF ≈ 1/727 ≈ 0.0014** — one hour of meeting audio diarizes in **~5 seconds** of engine time.
- **Aggregate ceiling on one MIG-2g.35gb slice: ~970x real time** (~16 audio-hours per wall-minute); saturation starts at 8-16 in-flight requests. Beyond that the 4-vCPU decode path (per-request ffmpeg) and the slice's GPU share are the bottleneck.
- Engine config behind the numbers: `cuda-fast` tier (2 s hop), 4 pooled CUDA streams (one per worker).
- 0 errors across all runs (1+4+8+16 requests; every run returned all 1440 segments).

Reproduce in-cluster (engine truth):

```bash
POD=$(kubectl -n adeo get pods -l oip/app=j-d4398150-0202-4e1c-ae9a-41be01288fd3 \
      -o jsonpath='{.items[0].metadata.name}')
kubectl cp tests/local/bench_diarize_k8s.py -n adeo $POD:/tmp/bench.py
kubectl cp 60min.wav -n adeo $POD:/tmp/bench.wav
kubectl exec -n adeo $POD -- sh -c \
  'CORO_BASE=http://127.0.0.1:8080 CORO_AUDIO=/tmp/bench.wav CORO_CONCURRENCY=8 \
   /home/runner/venv/bin/python /tmp/bench.py'
# columns: upload_s  MB/s  server_s  dl_s  total_s  e2e_RTFx  engine_RTFx  segs
```

### 3.2 Through-gateway stress (60s clips; includes network + gateway overhead)

Verified with `oicm-litellm-layer/scripts/probes/stress_diarize.py` (phases: gate -> contract -> single -> concurrency -> long clip) against the dev gateway port-forward, coro pod on `adeo-gpu-01` (4 CPU / 16 Gi / MIG-2g.35gb):

| Scenario | Result |
| -------- | ------ |
| Single 60s clip | 1.0-1.6s wall, 37-58x RTFx, 2 speakers detected |
| 8 clients x 5 rounds (40 reqs, 60s clips) | 0 errors, 40/40 succeeded, agg 58-79x RTFx |
| Concurrent latency (40 reqs) | p50 4.7-7.6s, p95 9.7-10.5s, p99 10.2-12.8s |
| 17.5 min AMI meeting (ES2004a, 1049s) | 19.6-29.9s wall, 35-54x RTFx, 5 speakers, 232 segments |
| Pod temp hygiene | 0 leftover upload temp files in pod `/tmp` after 42 requests |

Gateway-level numbers are transport-bounded: a `kubectl port-forward` tunnel moves audio at only ~2-6 MB/s (~0.6 MB/s under 4-way upload contention), so a 60-minute clip spends 15-60+ s uploading through the tunnel before the engine starts. In-cluster clients (the gateway included) upload the same 110 MiB body at loopback/pod-network speed: 0.3-2 s.

Key facts:

- Jobs are synchronous through the gateway: the HTTP response IS the completed job. There is no polling step.
- Throughput scales with the pod's diarization workers (4 workers, `cuda-fast`); aggregate concurrency beyond worker count queues rather than errors.
- For the same workload the engine alone reaches **727x single / 969x aggregate** (3.1); through-gateway RTFx is lower because it includes the audio transfer, which dominates on slow transports.

Reproduce through-gateway:

```bash
kubectl -n adeo-litellm port-forward svc/litellm-proxy-dev 14000:4000   # dev
GATEWAY_KEY=<your-api-key> \
DIARIZE_CONCURRENCY=8 DIARIZE_ROUNDS=5 \
  python3 oicm-litellm-layer/scripts/probes/stress_diarize.py
```

---

## 4. Route/field notes for clients

- The multipart file part must be named `file` (pyannoteAI contract).
- `model` omission relies on the gateway resolving the only registered diarization deployment; explicitly sending `model=pyannote/community-1` is the stable contract.
- `exclusive=true` fills `output.exclusiveDiarization`; without it that key is omitted (or null).
- The pod keeps the last 128 job ids in memory for `GET /v1/jobs/{jobId}` on the pod's own port; the gateway does not expose a job-status route (jobs are synchronous, so it is not needed).
- Spend logs: this route (like all audio-slice routes) does not write LiteLLM spend rows; usage tracking is not available for diarization yet.
