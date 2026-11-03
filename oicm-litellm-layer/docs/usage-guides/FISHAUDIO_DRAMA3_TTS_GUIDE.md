# Fish Audio Drama 3 (TTS): Curl Guide

> **Scope**: How to call Fish Audio Drama 3 text-to-speech (`fishaudio/drama-3`) through the LiteLLM gateway's `/v1/audio/speech` route, with sample curl commands, expected audio properties, error contracts, and the request fields the gateway currently drops
>
> **Related docs**: [FISHAUDIO_DRAMA3_REGISTRATION_DIAGNOSIS.md](../custom-providers/FISHAUDIO_DRAMA3_REGISTRATION_DIAGNOSIS.md) (root cause of both the dropped fields and the `chat` mode, plus the proposed fix), [INCEPTION_TTS_STT_GUIDE.md](INCEPTION_TTS_STT_GUIDE.md) (another OpenAI-shaped TTS model on the same route), [HAMSA-TTS-NEW-V1-GUIDE.md](HAMSA-TTS-NEW-V1-GUIDE.md) (Hamsa TTS)

---

## Gateway endpoint

| Capability | Gateway URL |
| ---------- | ----------- |
| Text to speech | `https://litellm.ecouncil.ae/v1/audio/speech` |

Authentication is the standard LiteLLM gateway key passed as a Bearer token. The sglang-omni pod itself does not require auth, but the gateway always requires it:

```
Authorization: Bearer <your-api-key>
```

The discovery controller registers the model as `fishaudio/drama-3` (upstream `hosted_vllm/fishaudio/drama-3`), so send exactly that id. Unlike diarization, `model` is mandatory here: the route rejects an omitted model with `Invalid model name passed in model=`.

---

## Model overview

| Property | Value |
| -------- | ----- |
| Model ID | `fishaudio/drama-3` |
| Underlying engine | sglang-omni `0.2.0-cu130-amd64`, stages `preprocessing` + `tts_engine` + `vocoder` |
| Type | Text to speech (35B MoE, 3B active, 10-codebook audio decoder) |
| Input | JSON body with text |
| Output | Binary audio, **44.1 kHz mono 16-bit** |
| Provider (upstream) | `hosted_vllm` |
| Deployed mode | `chat` (see [Mode caveat](#mode-caveat)) |

### Audio properties that break clients

Output is **44.1 kHz mono 16-bit** PCM in a WAV container. This is not the 24 kHz the OpenAI API produces. A client that hardcodes 24 kHz will play back at the wrong pitch and speed, roughly 1.8x too fast. Read the sample rate out of the WAV header, or declare it for raw `pcm`:

```bash
ffprobe -hide_banner out.wav   # expect 44100 Hz, 1 channel, 16-bit
```

`pcm` responses carry no header at all, so the 44.1 kHz / mono / 16-bit little-endian layout is the client's responsibility.

---

## 1. Synthesize speech

### Request shape

JSON body posted to `/v1/audio/speech`:

| Field | Required | Notes |
| ----- | -------- | ----- |
| `model` | yes | Must be `fishaudio/drama-3` |
| `input` | yes | The text to speak. Long text is chunked automatically by the pod |
| `voice` | **yes in practice** | Must be `"default"`, `""`, or a registered uploaded voice. The gateway rejects a missing `voice` before the pod sees the request (see below) |
| `response_format` | no | `wav` (default), `mp3`, `flac`, `pcm`, `aac`, `opus` |
| `speed` | no | Playback speed multiplier, honored end to end |
| `stream` | no | **Currently dropped by the gateway** (see §3) |
| `references` | no | **Currently dropped by the gateway** (see §3) |

`voice` is the field that catches people out. The gateway dispatches this model through its OpenAI TTS path, which requires a string `voice`, so omitting `voice` fails with:

```json
{"error":{"message":"litellm.BadRequestError: 'voice' is required to be passed as a string for OpenAI TTS ..."}}
```

Send `"voice": "default"` on every request. OpenAI voice names such as `alloy` are **not** accepted by the pod:

```json
{"error":{"message":"litellm.BadRequestError: Hosted_vllmException - Drama 3 request: unknown voice 'alloy'; use default or an uploaded voice ..."}}
```

### Sample curl

```bash
curl -s "https://litellm.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "fishaudio/drama-3",
    "input": "Hello, how are you today?",
    "voice": "default",
    "response_format": "wav"
  }' \
  -o out.wav
```

The response body is raw audio, so always redirect it to a file rather than letting it print to the terminal.

### Response shape

| Aspect | Value |
| ------ | ----- |
| HTTP status | `200` on success |
| `Content-Type` | Matches the format: `audio/wav`, `audio/mpeg`, `audio/flac`, `audio/pcm`, `audio/aac`, `audio/opus` |
| Body | Raw audio bytes. No JSON envelope |

All six formats were verified live through the gateway and each returned the expected container magic (`RIFF` for wav, `ID3` for mp3, `fLaC` for flac, `OggS` for opus).

### Verified example

A 25-character input produced 2.23 s of audio:

```
HTTP 200  audio/wav  196,652 bytes  44100 Hz  1 ch  16-bit  2.23 s
```

A 1,220-character input produced 95.11 s of audio in 15.1 s wall time, confirming that long text is chunked rather than truncated.

---

## 2. Parameters that work through the gateway

### `speed`

Honored all the way to the engine. The same sentence measured 2.83-3.62 s of audio at `speed: 1.0` and 1.60-1.70 s at `speed: 2.0` (three runs each, varying only by generation length). Through the gateway the same effect holds: 3.30 s of audio at `1.0` versus 1.53 s at `2.0`.

### `response_format`

Every documented value works. Use `flac` or `wav` when fidelity matters, `mp3`/`opus`/`aac` to save bandwidth, and `pcm` only when the client wants to handle the container itself.

---

## 3. Fields the gateway currently drops

This is the most important section, because these fields return HTTP 200 with perfectly valid audio while being silently ignored. Nothing in the response tells you the field was discarded.

| Field | Pod support | Through the gateway |
| ----- | ----------- | ------------------- |
| `stream` | Full. Requires `response_format: "pcm"` | **Ignored**; audio is always returned buffered and complete |
| `references` | Full. HTTP URL plus transcript | **Ignored**; output is the default voice |
| `voice` (aside from `default`) | Uploaded voices only | Forwarded, but no upload route is exposed (see §5) |

### How this was verified

Probing the pod directly with a deliberately invalid value and then repeating the same request through the gateway isolates the cause:

| Request | Pod direct | Gateway |
| ------- | ---------- | ------- |
| `stream: true` with `response_format: "wav"` | `400 stream=true requires response_format='pcm'` | `200 audio/wav` |
| `references` pointing at `https://example.invalid/nope.wav` | `400 Could not resolve media URL hostname: example.invalid` | `200 audio/wav` |

The pod rejects both; the gateway returns audio. The fields never arrive.

The mechanism is in the gateway itself, not the model config. `fishaudio/drama-3` is registered as `hosted_vllm`, and LiteLLM resolves no provider-specific TTS config for that provider, so `speech()` falls through to its OpenAI TTS branch. That branch builds the request from a fixed set of parameters (`model`, `input`, `voice`, `response_format`, `speed`, `instructions`) and passes the rest of `**kwargs` to logging rather than to the upstream call. Model-specific fields are still visible in gateway logs, which makes this easy to misdiagnose as a pod problem.

Consequence for callers: `stream: true` is accepted and returns 200, but TTFB equals total time because the whole file is generated and buffered first. A measured streaming request returned in 0.60 s TTFB / 0.76 s total with a complete 204,800-byte body and no `Transfer-Encoding: chunked`.

### Working around it

Voice cloning and streaming both work when you call the pod directly on the cluster network, which is what the deployment guide documents. Through the gateway today, treat the model as **non-streaming, default-voice TTS only**. If you need cloning or true streaming through the gateway, that needs a provider-specific TTS config for `hosted_vllm` (or a dedicated Drama 3 provider slice) so the request body is forwarded verbatim; it is a gateway change, not a client-side one.

---

## 4. Error contracts (verified live)

| Case | HTTP | Body |
| ---- | ---- | ---- |
| Missing/invalid gateway key | 401 | `Authentication Error, No api key passed in.` |
| `voice` omitted | 400 | `litellm.BadRequestError: 'voice' is required to be passed as a string for OpenAI TTS` |
| `voice: "alloy"` or another OpenAI name | 400 | `litellm.BadRequestError: Hosted_vllmException - Drama 3 request: unknown voice 'alloy'; use default or an uploaded voice` |
| `model` omitted | 400 | `{"detail":{"error":"/audio/speech: Invalid model name passed in model=. Call /v1/models to view available models for your key."}}` |
| Key without `/v1/audio/speech` in its `allowed_routes` | 403 | `Virtual key is not allowed to call this route. ...` |

Per-field validation errors pass through from the pod wrapped in `Hosted_vllmException`, with the pod's original message intact inside `error.message`. Unlike the diarize route, there is no unwrapping to a spec-shaped body; read the text inside the wrapper.

---

## 5. Voices and cloning

The pod maintains a voice registry, and it is reachable directly:

```
GET <pod>/v1/audio/voices
{"voices":["default"],"uploaded_voices":[],"cache_stats":{...}}
```

Through the gateway the same path returns `405 Method Not Allowed`, because `/v1/audio/voices` is claimed by the fork's voice-management router rather than proxied to this pod. Uploading a voice therefore needs direct pod access, and cloning through `references` is dropped anyway (§3), so the practical position today is: **use `default`**.

---

## 6. Mode caveat

The deployment is registered with `mode: chat`, not `text_to_speech`. The controller classifies a pod as chat whenever its `openapi.json` exposes `/v1/chat/completions`, and this sglang-omni pod exposes that path. Consequences:

- `POST /v1/chat/completions` with this model reaches the pod and fails with `Drama 3 request: the speech input must be text or an object with text and references`, because that route lowers `messages` to a list of dicts while the model needs an `input` string.
- `POST /v1/audio/speech` still works, because the route resolves by model name and the upstream base already ends in `/v1`.
- Anything that groups models by declared mode (dashboards, capability filters, auto-routers) will classify this model as a chat model.

The fix belongs in the controller's detection: `/v1/chat/completions` leads the pod's path list, so chat wins before `/v1/audio/speech` is considered. A pod that exposes speech should be registered as `text_to_speech` even when it also exposes a chat path it cannot usefully serve.

---

## 7. Performance

Measured on the Al Ain H200 through the gateway, single request, serial:

| Input | Output audio | Wall time | Notes |
| ----- | ------------ | --------- | ----- |
| 25 chars (`Hello, how are you today?`) | 1.67-2.18 s | 0.72-0.84 s | Three runs; length varies per generation |
| 52 chars (`speed: 1.0`) | 3.30 s | ~1.1 s | |
| Same at `speed: 2.0` | 1.53 s | ~1.1 s | |
| 1,220 chars | 95.11 s | 15.1 s | ~6.3x faster than realtime |

Generation length varies between identical requests, so compare durations as ranges rather than exact values. The pod's own benchmark notes (single H200, default placement) report 88.9 ms TTFA at concurrency 1 and 927 frames/s at concurrency 16. Through-gateway numbers are lower because the gateway buffers the whole response before returning it, so no audio reaches the client until generation finishes.

---

## 8. Route/field notes for clients

- Always send `voice: "default"`. Omitting it is a 400 even though the field is technically optional in the OpenAI schema.
- Always send `model: "fishaudio/drama-3"`. Omission is not defaulted on this route.
- Read the sample rate from the WAV header; do not assume 24 kHz.
- Do not rely on `stream: true`. It returns 200 with complete audio and no incremental delivery.
- Do not rely on `references` through the gateway. It is silently ignored.
- The pod's `/v1/audio/speech` rejects unknown fields outright rather than ignoring them: `style` and `previous_takes` fail with `unknown speech request field: <name>`, while `instructions` and `seed` fail with `<field> is not supported by this model`. This pod exposes only `/v1/audio/speech`, `/v1/audio/speech/batch`, and `/v1/audio/speech/{request_id}` on the speech surface, so cookbook features documented against other paths are not available here.
- `speed` is the one extra knob that genuinely works end to end.
- Spend logging: like the other audio-slice routes, this path does not write LiteLLM spend rows, so usage tracking for TTS is not available yet.

---

## 9. Deployment-side reference

The serving-side tuning doc (flags, concurrency tables, int4 checkpoint, sampling defaults, hard constraints such as the mandatory `triton` attention backend) lives outside this repo:

`mlops/oicm-custom-model-server-guide/docs/sglang/fishaudio-drama3.md`

Two of its claims do not hold through the gateway, and are worth knowing before you debug a request:

- Its `stream: true` examples assume `response_format: "pcm"` is required. That is true pod-direct and the pod does enforce it, but through the gateway `stream` never arrives, so neither the requirement nor the streaming behavior applies.
- Its voice-cloning examples use `references`. Those work pod-direct and are dropped by the gateway.
