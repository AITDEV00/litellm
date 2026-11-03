# Fish Audio Drama 3 (TTS): Curl Guide

> **Scope**: How to call Fish Audio Drama 3 text-to-speech (`fishaudio/drama-3`) through the LiteLLM gateway's `/v1/audio/speech` route, with sample curl commands, expected audio properties, error contracts, the direction-tag vocabulary that works, and the request fields the gateway currently drops
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
| Input | JSON body with text; direction tags inline (§4) |
| Output | Binary audio, **44.1 kHz mono 16-bit** |
| Provider (upstream) | `hosted_vllm` |
| Deployed mode | `audio_speech`, `oicm_task: Text-to-Speech` (see [Mode](#8-mode)) |
| Capabilities on the pod | voice cloning, persistent voices, direction tags, streaming, batch, sampling control |
| Capabilities through the gateway | default-voice non-streaming TTS plus direction tags (§3) |

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
| `input` | yes | The text to speak, **as a plain string**. Direction tags go inline (§4). Long text is chunked automatically by the pod |
| `voice` | **yes in practice** | Must be `"default"`, `""`, or a registered uploaded voice. The gateway rejects a missing `voice` before the pod sees the request (see below) |
| `response_format` | no | `wav` (default), `mp3`, `flac`, `pcm`, `aac`, `opus` |
| `speed` | no | Playback speed multiplier, honored end to end |
| `instructions` | no | Forwarded, but Drama 3 does not support it: the pod returns 400 |
| `stream` | no | **Dropped by the gateway**; pod supports it (§3, §6) |
| `references` | no | **Dropped by the gateway**; pod supports text-to-voice cloning (§3, §5) |

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

## 3. Fields the gateway drops

Drama 3 is a far more capable model than the gateway route exposes. Everything in
§4, §5 and §7 works on the pod and is unreachable through the gateway. Nothing in
the response tells you a field was discarded: you get HTTP 200 and valid audio for
the default voice.

| Field | Pod support | Through the gateway |
| ----- | ----------- | ------------------- |
| `references` (voice cloning) | Full: URL, base64, or pre-encoded VQ codes | **Dropped**; output is the default voice |
| `stream`, `stream_format` | Full: PCM passthrough or SSE `speech.audio.delta` | **Dropped**; audio always buffered and complete |
| `top_k`, `top_p`, `temperature` | Full | **Dropped**; engine defaults apply |
| `max_new_tokens` | Full | **Dropped** |
| `voice` other than `default` | Uploaded voices by name | Forwarded, but the upload route is not proxied (§5) |
| `instructions` | **Not supported by Drama 3** | Forwarded, and the pod rejects it |
| `[laughs]`, `<whisper>` and other tags | Full, in `input` text | **Work**, because they ride inside `input` |

The tag rows matter most in practice. Direction tags are part of the text, not a
separate field, so they survive the gateway while every structured control around
them is stripped.

### How this was verified

Probe the pod with a value it rejects, then repeat the identical request through
the gateway. A pod 400 with a gateway 200 proves the field never arrived.

| Request | Pod direct | Through the gateway |
| ------- | ---------- | ------------------- |
| `references` at `https://example.invalid/nope.wav` | `400 Could not resolve media URL hostname: example.invalid` | `200`, valid audio |
| `repetition_penalty: 1.5` | `400 repetition_penalty must be 1.0` | `200` |
| `task_type: "VoiceDesign"` | `400 only Base is` | `200` |
| `seed: 42` | `400 seed not supported` | `200` |
| `language: "English"` | `400 language not supported` | `200` |
| `instructions: "whisper"` | `400 instructions is not supported by this model` | `400`, same message |

`instructions` is the exception that proves the rule. It is on the gateway's
forwarding list, so the pod's rejection reaches you intact.

### Buffering, measured

`stream: true` is accepted and returns `200` with `content-type: audio/pcm`, which
makes it look like it worked. Time to first byte gives it away. Sending the same
`stream: true` request at three input lengths:

| Input length | TTFB | Total | Response |
| ------------ | ---- | ----- | -------- |
| one word | 0.43 s | 0.55 s | 69 KB |
| one sentence | 1.12 s | 1.50 s | 459 KB |
| one paragraph | 4.15 s | 5.34 s | 2.6 MB |

A streamed response has near-constant TTFB and a total that grows. Here TTFB grows
with the amount of audio generated, which means nothing is sent until generation
finishes. There is also no `transfer-encoding: chunked` header.

### The mechanism

`fishaudio/drama-3` is registered as `hosted_vllm`, and LiteLLM resolves no
provider-specific TTS config for that provider, so `speech()` falls through to its
OpenAI TTS branch. That branch assembles the outbound body from exactly three
optional parameters, in `litellm/main.py`:

```python
optional_params = {}
if response_format is not None:
    optional_params["response_format"] = response_format
if speed is not None:
    optional_params["speed"] = speed
if instructions is not None:
    optional_params["instructions"] = instructions
```

and the OpenAI handler then calls `client.audio.speech.create(model=..., voice=...,
input=..., **optional_params)`. Anything else you send is accepted, logged, and
never forwarded. Model-specific fields still appear in gateway logs, which makes
this easy to misdiagnose as a pod problem.

### Working around it

Call the pod directly for cloning, streaming, and sampling control. It needs no
authentication, so an in-cluster client or a port-forward is enough:

```bash
kubectl -n adeo port-forward deploy/j-51096511-94c6-4240-901a-86cccb5eb08a 8080:8080
```

Through the gateway, treat the model as **non-streaming, default-voice TTS with
direction tags only**. Forwarding the rest is a gateway change: a provider-specific
TTS config for `hosted_vllm` that passes the body through verbatim. It is not a
client-side workaround.

---

## 4. Direction tags and script control

Rich, and it works through the gateway. The model interprets bracketed directions
inline in the text; there is no fixed schema, so these are the forms worth using
rather than an exhaustive list.

### Point tags

Inserted where the event should land:

```
[laughs] [chuckles] [sighs] [gasps] [pause] [clears throat] [hesitates]
```

### Scoped tags

Wrap the span they apply to. These use angle brackets:

```
Promise me <whisper>you won't tell her</whisper>. [pause] Not a word.
```

`<slow>` and `<emphasis>` work the same way.

### Free-form style directions

Bracketed prose, describing delivery rather than a named tag:

```
[with bright eager excitement] We won! We actually won! [laughs] I can't believe it!
[breathless, rising excitement] He's past one... past two... he shoots, and [shouting] HE SCORES!
```

### Direction and Line

For richer scenes, split the direction from the spoken text. This is how the
upstream Drama 3 Space lets you steer a take without polluting the audio:

```
Direction: icy calm, as if you already accepted it. Put the rise on "goodbye".
Line: So this is goodbye.
```

Verification that tags are performed rather than read aloud, measured through the
gateway on the same sentence with and without tags, three runs each:

| Input | Audio duration |
| ----- | -------------- |
| `[sighs] Fine. If we miss it, it is on you. [laughs] Well, maybe that is fair.` | 8.73 - 9.80 s |
| `Fine. If we miss it, it is on you. Well, maybe that is fair.` | 7.06 - 7.80 s |

The tags add roughly 1.5 s of non-speech audio and the wording is identical, so
the sigh and the laugh are performed. The comparison is the evidence, not the
absolute durations, which vary between runs.

---

## 5. Voice cloning and voice management

Both exist and both work on the pod. Neither is reachable through the gateway.

### Inline cloning with `references`

Top-level `references` is a sibling of `input`, not a field inside it. `input`
stays a plain string; this is the single most common mistake, and it fails with
`400 input must be a string`.

Each reference supplies the audio and its transcript. Three source forms are
accepted:

| Form | Field |
| ---- | ----- |
| HTTP(S) URL or pod-local path | `audio_path` |
| Inline base64 | `data` (or `base64`) plus `media_type` |
| Pre-encoded codes | `vq_codes`, skipping the encode step entirely |

```bash
curl -s -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{
    "input": "Get the trust fund to the bank early.",
    "references": [{
      "audio_path": "https://huggingface.co/datasets/zhaochenyang20/seed-tts-eval-mini/resolve/main/en/prompt-wavs/common_voice_en_10119832.wav",
      "text": "We asked over twenty different people, and they all said it was his."
    }],
    "response_format": "wav"
  }' -o cloned.wav
```

Verified: HTTP 200, 2.51 s of 44.1 kHz mono audio. Supplying `references` to the
gateway instead returns a different random default voice, because the field is
dropped.

Reference limits, all enforced before encoding:

| Limit | Value | Error |
| ----- | ----- | ----- |
| Per-reference duration | 60 s | `reference audio lasts 75.0 s, longer than the 60 s limit` |
| Prompt rows | 8192 (about 21.5 rows per second of reference audio) | `a prompt of 8193 rows exceeds the maximum allowed length` |

The 60 s cap is raised at server start with
`--preprocessing.factory.max_reference_audio_seconds`, which also needs a matching
`--preprocessing.total_reserve_bytes` or the preprocessing stage runs out of GPU
memory.

### Persistent voices

The pod has a full voice registry, so you can upload a voice once and refer to it
by name on every later request.

```bash
# list
curl -s http://localhost:8080/v1/audio/voices

# upload (multipart)
curl -s -X POST http://localhost:8080/v1/audio/voices \
  -F "audio_sample=@my_voice.wav;type=audio/wav" \
  -F "consent=I have permission to use this voice." \
  -F "name=Narrator_01" \
  -F "ref_text=A sample reference transcript."

# use it by name
curl -s -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input": "Speaking with the uploaded voice.", "voice": "Narrator_01"}'

# delete
curl -s -X DELETE http://localhost:8080/v1/audio/voices/Narrator_01
```

Verified end to end: upload returned the voice metadata, a request naming
`Narrator_01` returned 200 with audio, and delete removed it from the registry.

Upload constraints:

| Constraint | Value |
| ---------- | ----- |
| Duration | 1 s to 30 s, mono, non-silent |
| Max voices | 1000 (`SPEAKER_MAX_UPLOADED`) |
| Name | `[A-Za-z0-9_.-]+`, at least one alphanumeric, `default` is reserved |
| Formats | wav, mp3, flac, ogg, opus, aac, m4a/mp4, webm |
| `consent` | Required, no default |

Uploaded voices persist across restarts as safetensors files under
`~/.cache/sglang-omni/speakers`, so a `restartPolicy`-driven pod restart keeps
them and a pod replacement does not.

### Through the gateway

Every voice-management verb fails at the gateway, and the two verbs fail
differently, which is worth knowing before you read a status code as "the pod is
down".

| Verb | Result | Why |
| ---- | ------ | --- |
| `GET /v1/audio/voices` | `405 Method Not Allowed` | The gateway's own voice router owns the path and has no GET |
| `POST /v1/audio/voices` (multipart) | `500 Internal server error` | The gateway's handler reads the body as JSON, so a multipart upload fails with `RuntimeError: Stream consumed` |
| `POST /v1/audio/speech/batch` | `404 Not Found` | Not a route the gateway exposes |

The `500` is a gateway defect, not a bad request: `litellm/proxy/voice_routes.py`
calls `await request.body()` and then `orjson.loads(body)`, so only a JSON body
can ever be parsed. A browser or curl multipart upload, which is what the pod
itself accepts, cannot work through the gateway at all.

The gateway's voice router is also a different feature from the pod's registry. It
forwards a JSON body to a provider-side `acreate_voice` action, so it does not
manage this pod's safetensors voices even when it succeeds.

Since `references` is dropped as well, the practical position through the gateway
is **use `default`**. Any other name reaches the pod and is rejected:

```json
{"error":{"message":"litellm.BadRequestError: Hosted_vllmException - Drama 3 request: unknown voice 'alloy'; use default or an uploaded voice ..."}}
```

---

## 6. Sampling parameters and limits

These are pod-only; the gateway drops them (§3).

| Parameter | Default | Notes |
| --------- | ------- | ----- |
| `temperature` | 1.0 | |
| `top_p` | 0.9 | |
| `top_k` | no cap on the main token | The codebook heads cap at 30 regardless, because wider caps cost speaker similarity. A main-token cap of 30 makes a stream fall silent after its first phrase |
| `max_new_tokens` | model default | |
| `repetition_penalty` | 1.0 | **Must be 1.0**; anything else is a 400 |
| `speed` | 1.0 | 0.25 to 4.0 |

Rejected outright, with the pod naming the field: `seed`, `language`,
`instructions`, `task_type` other than `Base`, `x_vector_only_mode`,
`stream_codec_output`, `suppress_bootstrap_silence`, `token_count`,
`duration_tokens`, `initial_codec_chunk_frames`, `stage_params`.

`seed` being unsupported is worth noting for anyone hoping to make takes
reproducible: they cannot be, at the request level.

### Multi-speaker is not available

Drama 3 can carry up to 16 characters (`<|speaker:0|>` to `<|speaker:15|>`), and
the prompt builder handles all of them, but the HTTP request path hard-restricts
to speaker 0:

```json
{"error":{"message":"Drama 3 request: only speaker 0 is supported; got speaker tags [1]"}}
```

This is a code restriction in `speech_request.py`, not a config knob, so a
multi-character scene needs one request per character and then concatenation.

### Streaming

Pod-only. Requires `response_format: "pcm"`; a `wav` container states a total
length and cannot be cut into chunks. `stream_format` selects the envelope:

```bash
# raw PCM passthrough (default)
curl -sN -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"...","stream":true,"response_format":"pcm"}' -o out.pcm

# SSE events
curl -sN -X POST http://localhost:8080/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"...","stream":true,"stream_format":"sse","response_format":"pcm"}'
```

SSE events are `{"type": "speech.audio.delta", "audio": "<base64>"}` terminated by
a `speech.audio.done` event.

Beyond `/v1/audio/speech`, the pod also exposes `POST /v1/audio/speech/batch` for
multiple inputs in one request (verified working, max 32 items by default) and a
stateful `WS /v1/audio/speech/stream`. None are proxied by the gateway.

---

## 7. Error contracts (verified live)

| Case | HTTP | Body |
| ---- | ---- | ---- |
| Missing/invalid gateway key | 401 | `Authentication Error, No api key passed in.` |
| `voice` omitted | 400 | `litellm.BadRequestError: 'voice' is required to be passed as a string for OpenAI TTS` |
| `voice: "alloy"` or another OpenAI name | 400 | `litellm.BadRequestError: Hosted_vllmException - Drama 3 request: unknown voice 'alloy'; use default or an uploaded voice` |
| `model` omitted | 400 | `{"detail":{"error":"/audio/speech: Invalid model name passed in model=. Call /v1/models to view available models for your key."}}` |
| Key without `/v1/audio/speech` in its `allowed_routes` | 403 | `Virtual key is not allowed to call this route. ...` |
| `input` is an object rather than a string | 400 | `litellm.BadRequestError: Hosted_vllmException - input must be a string` |
| `repetition_penalty` other than 1.0 | 400 | `Drama 3 request: repetition_penalty must be 1.0` |
| `instructions` set | 400 | `Drama 3 request: instructions is not supported by this model` |
| Reference longer than 60 s | 400 | `references.N: reference audio lasts X s, longer than the 60 s limit` |
| Speaker tag other than 0 | 400 | `Drama 3 request: only speaker 0 is supported; got speaker tags [...]` |
| `POST /v1/audio/voices` with multipart | 500 | `Internal server error` (gateway bug, see §5) |
| `GET /v1/audio/voices` | 405 | `{"detail":"Method Not Allowed"}` |
| `POST /v1/audio/speech/batch` | 404 | `{"detail":"Not Found"}` |

Per-field validation errors pass through from the pod wrapped in `Hosted_vllmException`, with the pod's original message intact inside `error.message`. Unlike the diarize route, there is no unwrapping to a spec-shaped body; read the text inside the wrapper.

---

## 8. Mode

The deployment registers as `mode: audio_speech` with `oicm_task: Text-to-Speech`
and `health_check_voice: default`. This was not always the case: before
2026-11-03 it registered as `chat`, because the controller classified any pod
whose `openapi.json` exposes `/v1/chat/completions` as a chat model and this
sglang-omni pod exposes that path.

The fix came from OICM's declared `inference_task`, which the operator sets at
deploy time, gated on the pod actually mounting the task's native surface. This
pod declares `Text-to-Speech` and mounts `/v1/audio/speech`, so the declaration
wins. A pod that declares a non-chat task but does not mount its surface keeps
the mounted mode, with the declaration preserved as `model_info.oicm_task`. See
[CAPABILITY_REGISTRATION_REVIEW2.md](../custom-providers/CAPABILITY_REGISTRATION_REVIEW2.md#shipped-state-2026-11-03).

Consequences for callers:

- `POST /v1/audio/speech` is the correct route, as documented throughout this
  guide.
- `POST /v1/chat/completions` with this model still reaches the pod and still
  fails with `Drama 3 request: the speech input must be text or an object with
  text and references`. The mode label does not disable that route; the pod
  simply cannot serve it.
- Anything that groups models by declared mode now classifies this model as a
  speech model.

---

## 9. Performance

Measured on the Al Ain H200 through the gateway, single request, serial:

| Input | Output audio | Wall time | Notes |
| ----- | ------------ | --------- | ----- |
| 25 chars (`Hello, how are you today?`) | 1.67-2.18 s | 0.72-0.84 s | Three runs; length varies per generation |
| 52 chars (`speed: 1.0`) | 3.30 s | ~1.1 s | |
| Same at `speed: 2.0` | 1.53 s | ~1.1 s | |
| 1,220 chars | 95.11 s | 15.1 s | ~6.3x faster than realtime |

Generation length varies between identical requests, so compare durations as ranges rather than exact values. The pod's own benchmark notes (single H200, default placement) report 88.9 ms TTFA at concurrency 1 and 927 frames/s at concurrency 16. Through-gateway numbers are lower because the gateway buffers the whole response before returning it, so no audio reaches the client until generation finishes.

---

## 10. Route/field notes for clients

- Always send `voice: "default"`. Omitting it is a 400 even though the field is technically optional in the OpenAI schema.
- Always send `model: "fishaudio/drama-3"`. Omission is not defaulted on this route.
- Read the sample rate from the WAV header; do not assume 24 kHz.
- Direction tags (`[laughs]`, `<whisper>`, `Direction:`/`Line:`) work through the gateway because they ride inside `input`. They are the only advanced control that survives (§4).
- Do not rely on `stream: true` through the gateway. It returns 200 with complete audio and no incremental delivery. Pod-direct it works (§6).
- Do not rely on `references` through the gateway. It is silently ignored. Pod-direct it clones (§5).
- `input` must be a string. Putting references or takes inside it fails with `input must be a string`.
- `speed` and `response_format` are the only two optional knobs that work end to end through the gateway.
- Spend logging: like the other audio-slice routes, this path does not write LiteLLM spend rows, so usage tracking for TTS is not available yet.

---

## 11. Deployment-side reference

The serving-side tuning doc (flags, concurrency tables, int4 checkpoint, sampling defaults, hard constraints such as the mandatory `triton` attention backend) lives outside this repo:

`mlops/oicm-custom-model-server-guide/docs/sglang/fishaudio-drama3.md`

Two of its claims do not hold through the gateway, and are worth knowing before you debug a request:

- Its `stream: true` examples assume `response_format: "pcm"` is required. That is true pod-direct and the pod does enforce it, but through the gateway `stream` never arrives, so neither the requirement nor the streaming behavior applies.
- Its voice-cloning examples use `references`. Those work pod-direct and are dropped by the gateway.

The authoritative upstream sources are the SGLang-Omni cookbook
(`docs/cookbook/fishaudio_drama3.md` in `sglang-omni`, pinned at tag `v0.2.0`,
the version this pod runs) and
`sglang_omni/models/fishaudio_drama3/speech_request.py`, which holds the exact
accepted and rejected field list this guide's §3 and §6 are derived from.
