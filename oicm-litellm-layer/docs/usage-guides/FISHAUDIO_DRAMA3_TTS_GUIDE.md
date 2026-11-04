# Fish Audio Drama 3 (TTS): Curl Guide

> **Scope**: How to call Fish Audio Drama 3 text-to-speech (`fishaudio/drama-3`) through the LiteLLM gateway's `/v1/audio/speech` route, with sample curl commands, expected audio properties, error contracts, the direction-tag vocabulary that works, and which request fields the gateway forwards
>
> **Related docs**: [FISHAUDIO_DRAMA3_REGISTRATION_DIAGNOSIS.md](../custom-providers/FISHAUDIO_DRAMA3_REGISTRATION_DIAGNOSIS.md) (root cause of the original dropped fields and the `chat` mode, plus the proposed fix), [INCEPTION_TTS_STT_GUIDE.md](INCEPTION_TTS_STT_GUIDE.md) (another OpenAI-shaped TTS model on the same route), [HAMSA-TTS-NEW-V1-GUIDE.md](HAMSA-TTS-NEW-V1-GUIDE.md) (Hamsa TTS)

---

## Gateway endpoints

Every route below was exercised against the production gateway
(`https://litellm.ecouncil.ae`) with a `key_type=llm_api` virtual key, not the
master key, so the status codes are what a customer sees.

| Method | Path | Purpose | Verified |
| ------ | ---- | ------- | -------- |
| `POST` | `/v1/audio/speech` | Synthesize speech (all formats, streaming, cloning) | 200 |
| `POST` | `/v1/audio/voices` | Upload a voice (multipart) | 200 |
| `DELETE` | `/v1/voices/profiles/{name}` | Delete an uploaded voice | 200 |
| `GET` | `/v1/voices` | List voices (see caveat below) | 200 |
| `GET` | `/v1/voices/profiles` | Alias of the same listing | 200 |
| `GET` | `/v1/voices/profiles/{name}` | Fetch one profile | 200 |
| `GET` | `/v1/models` | Confirm the model is visible to your key | 200 |
| `POST` | `/v1/audio/speech/batch` | Batch synthesis | **404**, not proxied |

Base URL for every example: `https://litellm.ecouncil.ae`.

### The one real trap: GET voice routes cannot be model-scoped

`GET /v1/voices` and its aliases read the model from the request body, which a
`GET` does not have. A `?model=` query parameter is **ignored** by the gateway,
so a no-model GET resolves to whichever voice-capable deployment the router picks
first. On production that is OmniVoice, whose registry returns 31 entries
(`auto`, `design:<attributes>`, OpenAI presets like `alloy`, and `clone:adeo_*`
profiles) rather than Drama 3's single `default` voice:

```bash
curl -s "https://litellm.ecouncil.ae/v1/voices" -H "Authorization: Bearer $KEY"
# {"voices":[{"id":"auto",...},{"id":"alloy",...},{"id":"clone:adeo_ahmed",...}]}
```

Do not read that as Drama 3 state. To inspect Drama 3's registry, probe it through
a request that carries a body (`POST /v1/audio/voices` upload, or a speech request
naming a voice), or call the pod directly (§12). The `DELETE` route does carry a
body, so `-d '{"model":"fishaudio/drama-3"}'` scopes it correctly and is what the
example in §6 uses.

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
| Input | JSON body with text; direction tags inline (§5) |
| Output | Binary audio, **44.1 kHz mono 16-bit** |
| Provider (upstream) | `hosted_vllm` |
| Deployed mode | `audio_speech`, `oicm_task: Text-to-Speech` (see [Mode](#9-mode)) |
| Capabilities on the pod | voice cloning, persistent voices, direction tags, streaming, batch, sampling control |
| Capabilities through the gateway | all of them except batch (§4, §6, §7) |

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
| `input` | yes | The text to speak, **as a plain string**. Direction tags go inline (§5). Long text is chunked automatically by the pod |
| `voice` | no | Defaults to the default voice when omitted. `"default"`, or a registered uploaded voice name (§6). OpenAI voice names such as `alloy` are rejected by the pod |
| `response_format` | no | `wav` (default), `mp3`, `flac`, `pcm`, `aac`, `opus` |
| `speed` | no | Playback speed multiplier, honored end to end |
| `instructions` | no | Forwarded, but Drama 3 does not support it: the pod returns 400 |
| `stream` | no | Forwarded; requires `response_format: "pcm"` (the pod rejects stream with a container format). Delivery is chunked (§7) |
| `references` | no | Forwarded; inline voice cloning (§6) |

The gateway dispatches this model through its `drama3` provider path, which forwards every non-LiteLLM field to the pod. The pod is strict about unknown fields, so a misspelled field is a 400 (`unknown speech request field`), never a silent drop. See §4 for the forwarding contract.

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

All six formats verified on production with a virtual key, same input
(`"Hello there."`), correct container magic in every case:

| `response_format` | HTTP | Bytes | Content-Type | Magic |
| ----------------- | ---- | ----- | ------------ | ----- |
| `wav` | 200 | 73,772 | `audio/wav` | `RIFF` |
| `mp3` | 200 | 9,839 | `audio/mpeg` | `ID3` |
| `flac` | 200 | 54,128 | `audio/flac` | `fLaC` |
| `pcm` | 200 | 81,920 | `audio/pcm` | raw samples |
| `aac` | 200 | 8,271 | `audio/aac` | ADTS `FFF1` |
| `opus` | 200 | 7,802 | `audio/opus` | `OggS` |

An unknown `response_format` is rejected by the pod rather than silently
defaulted, consistent with the strict-field behaviour in §4.

### Verified example

A 25-character input produced 2.23 s of audio:

```
HTTP 200  audio/wav  196,652 bytes  44100 Hz  1 ch  16-bit  2.23 s
```

A 1,220-character input produced 95.11 s of audio in 15.1 s wall time, confirming that long text is chunked rather than truncated.

Omitting `voice` entirely is also valid and synthesizes with the default voice (verified live: HTTP 200, RIFF audio).

---

## 2. Parameters that work through the gateway

### `speed`

Honored all the way to the engine. The same sentence measured 2.83-3.62 s of audio at `speed: 1.0` and 1.60-1.70 s at `speed: 2.0` (three runs each, varying only by generation length). Through the gateway the same effect holds: 3.30 s of audio at `1.0` versus 1.53 s at `2.0`.

Verified on production as a monotonic size response to the same input string:

| `speed` | Bytes | Relative |
| ------- | ----- | -------- |
| `0.5` | 499,756 | 2.07x |
| `1.0` | 241,708 | 1.00x |
| `2.0` | 139,308 | 0.58x |

Audio length scales with `1/speed` as expected, so the field reaches the engine
rather than being dropped and re-defaulted.

### `response_format`

Every documented value works. Use `flac` or `wav` when fidelity matters, `mp3`/`opus`/`aac` to save bandwidth, and `pcm` only when the client wants to handle the container itself (and for streaming, §7).

### Sampling controls

`temperature`, `top_k`, `top_p` and `max_new_tokens` are forwarded and accepted (verified live: HTTP 200 with `top_k: 30`, `temperature: 0.8`, `max_new_tokens: 500`). `repetition_penalty` is forwarded too, with the pod's own constraint that it must be exactly `1.0`; anything else is a 400 naming the field (§8).

---

## 3. Every way to use it

One block per usage mode, all verified against production. Set these first:

```bash
KEY="Authorization: Bearer <your-api-key>"
BASE=https://litellm.ecouncil.ae
```

### A. Basic synthesis, default voice

```bash
curl -s -X POST "$BASE/v1/audio/speech" \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{"model":"fishaudio/drama-3","input":"Hello there.","voice":"default","response_format":"wav"}' \
  -o out.wav
```

`voice` may be omitted entirely; `response_format` defaults to `wav`.

### B. Choose a container

Swap `response_format` for `mp3`, `flac`, `pcm`, `aac`, or `opus`. Sizes for the
same input are in §1.

### C. Control pace

```bash
-d '{"model":"fishaudio/drama-3","input":"Take your time with this.","voice":"default","speed":0.75}'
```

### D. Steer the delivery with direction tags

```bash
-d '{"model":"fishaudio/drama-3","voice":"default","input":"[sighs] Fine. If we miss it, it is on you. [laughs] Well, maybe that is fair."}'
```

Scoped tags (`<whisper>...</whisper>`), free-form prose directions
(`[with bright eager excitement]`), and the `Direction:`/`Line:` split all work the
same way. See §5.

### E. Tune sampling

```bash
-d '{"model":"fishaudio/drama-3","input":"Sampling probe.","voice":"default","top_k":30,"temperature":0.8,"top_p":0.9,"max_new_tokens":500}'
```

`repetition_penalty` must be exactly `1.0` if you send it at all.

### F. Stream audio

Requires `pcm`. Use `-N` so curl writes as it receives.

```bash
curl -sN -X POST "$BASE/v1/audio/speech" \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{"model":"fishaudio/drama-3","input":"A long line to stream.","voice":"default","stream":true,"response_format":"pcm"}' \
  -o out.pcm
```

Add `"stream_format":"sse"` for `speech.audio.delta` events.

### G. Clone a voice inline from a URL

```bash
-d '{"model":"fishaudio/drama-3","input":"Cloned from a URL.","voice":"default","references":[{"audio_path":"https://reachable.host/ref.wav","text":"Transcript of the reference."}]}'
```

### H. Clone a voice inline from base64 (no egress needed)

Preferred inside this cluster, which has no internet access. Write the body to a
file; a ~1 MB base64 body inline on the command line hits `Argument list too long`.

```bash
base64 -w0 my_voice.wav > /tmp/b64.txt
python3 -c "
import json
b64=open('/tmp/b64.txt').read().strip()
json.dump({'model':'fishaudio/drama-3','input':'Cloned inline.','voice':'default',
           'references':[{'data':b64,'media_type':'audio/wav','text':'The transcript.'}]},
          open('/tmp/ref.json','w'))
"
curl -s -X POST "$BASE/v1/audio/speech" \
  -H "$KEY" -H "Content-Type: application/json" \
  --data-binary @/tmp/ref.json -o cloned.wav
```

### I. Register a reusable voice, then speak with it

Both the file field (`ref_audio`) and `model` are required, and `model` is what
picks Drama 3 rather than another voice-capable pod.

```bash
curl -s -X POST "$BASE/v1/audio/voices" -H "$KEY" \
  -F "model=fishaudio/drama-3" \
  -F "ref_audio=@my_voice.wav;type=audio/wav" \
  -F "consent=I have permission to use this voice." \
  -F "name=Narrator_01" \
  -F "ref_text=The transcript of my voice sample."

curl -s -X POST "$BASE/v1/audio/speech" \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{"model":"fishaudio/drama-3","input":"Speaking as the registered voice.","voice":"Narrator_01"}' \
  -o out.wav
```

### J. Inspect and delete registered voices

```bash
# delete (body scopes it to Drama 3)
curl -s -X DELETE "$BASE/v1/voices/profiles/Narrator_01" \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{"model":"fishaudio/drama-3"}'

# fetch one profile
curl -s "$BASE/v1/voices/profiles/Narrator_01" -H "$KEY"

# listing caveat: ?model= is ignored, so this answers with another pod's registry
curl -s "$BASE/v1/voices" -H "$KEY"
```

### K. Confirm the model is visible to your key

```bash
curl -s "$BASE/v1/models" -H "$KEY" | grep -o 'fishaudio/drama-3'
```

### L. Batch synthesis (pod-direct only)

404 through the gateway; see §12 for the port-forward.

```bash
curl -s -X POST http://localhost:8080/v1/audio/speech/batch \
  -H "Content-Type: application/json" \
  -d '{"items":[{"input":"one","voice":"default"},{"input":"two","voice":"default"}]}'
```

### Production verification summary

Every mode above was run against `https://litellm.ecouncil.ae` with a
`key_type=llm_api` virtual key:

| Mode | Result |
| ---- | ------ |
| A basic wav | 200, 73,772 B, `RIFF` |
| B all six formats | 200 each, correct magic |
| C `speed` 0.5 / 1.0 / 2.0 | 200, 499,756 / 241,708 / 139,308 B |
| D direction tags | 200, 9.85 s vs 6.22 s untagged |
| E sampling | 200 |
| F streaming pcm | 200, TTFB 0.41 s vs total 1.34 s |
| G inline URL clone | 400 on unreachable host (forwarding proven), 200 on reachable |
| H inline base64 clone | 200, 208,940 B, `RIFF` |
| I register + speak | 200 upload, 200 speech at 44.1 kHz |
| J delete + probe | 200 delete, 400 on the deleted voice |
| K `/v1/models` | model listed for the key |
| L batch | 404 via gateway (pod-direct only) |


---

## 4. The forwarding contract

Drama 3 is registered with a provider-specific TTS config (`litellm/llms/drama3/`), so the gateway no longer routes it through the OpenAI TTS branch. The provider transformation forwards the model, `input`, `voice`, `response_format`, `speed`, and every Drama 3 schema field (references, sampling controls, `stream`, `stream_format`) to the pod, and filters out only LiteLLM's own internal kwargs (model resolution, routing, logging).

The observable consequence: the pod's validation reaches you intact. A field the pod rejects produces a 400 through the gateway, and a field the pod accepts changes the audio.

### How this was verified

Probe with a value the pod rejects; if the pod's error comes back through the gateway, the field arrived. All of these were run through the gateway and returned the pod's own 400:

| Request | Through the gateway |
| ------- | ------------------- |
| `references` at `https://example.invalid/nope.wav` | `400 Could not resolve media URL hostname: example.invalid` |
| `repetition_penalty: 1.5` | `400 Drama 3 request: repetition_penalty must be 1.0` |
| `task_type: "VoiceDesign"` | `400 only Base is` |
| `seed: 42` | `400 seed not supported` |
| `language: "English"` | `400 language not supported` |
| `instructions: "whisper"` | `400 instructions is not supported by this model` |
| `stream: true` with `response_format: "wav"` | `400 stream=true requires response_format='pcm'` |

Before the provider slice existed, every row except `instructions` returned `200` with valid default-voice audio: the gateway silently dropped the field. That is the failure mode the sentinel method detects, and the one this contract now prevents.

Fields the pod refuses outright with `unknown speech request field` are likewise forwarded, so a misspelled field name fails loudly on both sides of the gateway instead of being swallowed.

### Streaming, measured

`stream: true` with `response_format: "pcm"` delivers incrementally. Measured on
production:

| Input length | TTFB | Total | Size |
| ------------ | ---- | ----- | ---- |
| one word (`Hi.`) | 0.41 s | 0.53 s | 72 KB |
| one sentence | 0.40 s | 1.34 s | 564 KB |

Time to first byte stays flat around 0.4 s while the total grows with the audio,
so audio reaches the client while generation continues. A buffered response would
show TTFB growing alongside the total. Note that `transfer-encoding: chunked` is
not present in the captured headers: the proxy streams through `StreamingResponse`
with an explicit status and no `Content-Length`, so chunking is implicit in HTTP/1.1
rather than advertised. Judge streaming by TTFB behaviour, not by that header.

The `stream_format` field rides along too (`audio` for raw PCM passthrough, `sse` for `speech.audio.delta` events terminated by `speech.audio.done`).

### The mechanism

`speech()` builds its OpenAI-branch body from exactly three optional parameters (`response_format`, `speed`, `instructions`). Everything else a client sends arrives as `**kwargs`. The `drama3` transformation class declares the Drama 3 schema fields as forwardable and additionally filters LiteLLM's own kwargs with the canonical `is_litellm_owned_kwarg` predicate, so router-injected parameters (deployment settings, keepalive overrides) cannot leak into the pod body either. The dispatch tail in `litellm/main.py` routes `drama3` to the generic HTTP handler with this config.

---

## 5. Direction tags and script control

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
gateway on the same sentence with and without tags:

| Input | Audio duration |
| ----- | -------------- |
| `[sighs] Fine. If we miss it, it is on you. [laughs] Well, maybe that is fair.` | 8.73 - 9.85 s |
| `Fine. If we miss it, it is on you. Well, maybe that is fair.` | 6.22 - 7.80 s |

Reproduced on production (9.85 s tagged vs 6.22 s plain for one pair), so the tags
add roughly 2-3 s of non-speech audio while the wording is identical: the sigh and
the laugh are performed. Compare durations as ranges rather than absolute values,
because generation length varies between runs. The comparison is the evidence.

---

## 6. Voice cloning and voice management

Both exist and both work through the gateway. Inline cloning rides the speech request (`references`); persistent voices ride the gateway's voice-management routes, which forward to the pod's own registry.

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
curl -s "https://litellm.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "fishaudio/drama-3",
    "input": "Get the trust fund to the bank early.",
    "references": [{
      "audio_path": "https://example.com/prompt.wav",
      "text": "We asked over twenty different people, and they all said it was his."
    }],
    "response_format": "wav"
  }' -o cloned.wav
```

Verified live through the gateway: an unresolvable reference URL returns the pod's own `400 Could not resolve media URL hostname`, which proves the field is forwarded. A resolvable reference clones the voice.

**Use inline base64 when the pod has no egress.** The cluster has no internet
access, so public dataset URLs (Hugging Face and similar) either 404 or hang
inside the pod. Base64 sidesteps the fetch entirely, because the audio arrives in
the request body:

```bash
KEY="Authorization: Bearer <your-api-key>"
B64=$(base64 -w0 my_voice.wav)

python3 - <<'PY'
import json, os
b64 = open("/tmp/b64.txt").read().strip()
body = {
    "model": "fishaudio/drama-3",
    "input": "Cloned inline without any upload.",
    "voice": "default",
    "references": [{
        "data": b64,
        "media_type": "audio/wav",
        "text": "The transcript of the reference clip.",
    }],
}
open("/tmp/ref.json", "w").write(json.dumps(body))
PY

curl -s -X POST "https://litellm.ecouncil.ae/v1/audio/speech" \
  -H "$KEY" -H "Content-Type: application/json" \
  --data-binary @/tmp/ref.json -o cloned.wav
```

Verified on production: a ~1 MB base64 body (a 775 KB wav) returned `200` with
44.1 kHz mono RIFF audio. Write the JSON to a file and use `--data-binary @file`;
a 1 MB body inline on the command line hits `Argument list too long`.

Reference limits, all enforced before encoding:

| Limit | Value | Error |
| ----- | ----- | ----- |
| Per-reference duration | 60 s | `reference audio lasts 75.0 s, longer than the 60 s limit` |
| Prompt rows | 8192 (about 21.5 rows per second of reference audio) | `a prompt of 8193 rows exceeds the maximum allowed length` |

The 60 s cap is raised at server start with
`--preprocessing.factory.max_reference_audio_seconds`, which also needs a matching
`--preprocessing.total_reserve_bytes` or the preprocessing stage runs out of GPU
memory.

### Persistent voices through the gateway

The pod has a full voice registry, and the gateway's voice-management routes now
proxy it. Two details decide whether the call targets Drama 3 or the wrong pod:

- the file field is named **`ref_audio`** (the gateway forwards it to the pod as
  `audio_sample`), and sending `audio_sample` instead fails with
  `'ref_audio' is required for Drama 3 voice registration`
- the model must ride **`model=` as a form field** on the upload. Without it the
  router picks the first voice-capable deployment, which on production is
  OmniVoice, and the voice lands in the wrong registry

```bash
KEY="Authorization: Bearer <your-api-key>"
BASE=https://litellm.ecouncil.ae

# upload (multipart) -- note: ref_audio + model as form fields
curl -s -X POST "$BASE/v1/audio/voices" \
  -H "$KEY" \
  -F "model=fishaudio/drama-3" \
  -F "ref_audio=@my_voice.wav;type=audio/wav" \
  -F "consent=I have permission to use this voice." \
  -F "name=Narrator_01" \
  -F "ref_text=A sample reference transcript."

# use it by name
curl -s -X POST "$BASE/v1/audio/speech" \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{"model":"fishaudio/drama-3","input":"Speaking with the uploaded voice.","voice":"Narrator_01"}' \
  -o out.wav

# delete -- the body is what scopes this to Drama 3
curl -s -X DELETE "$BASE/v1/voices/profiles/Narrator_01" \
  -H "$KEY" -H "Content-Type: application/json" \
  -d '{"model":"fishaudio/drama-3"}'
```

Verified end to end on production:

| Step | Result |
| ---- | ------ |
| upload | `200` with `{"name","consent","created_at","file_size","mime_type","ref_text"}`, `model_id` = the Drama 3 row |
| speech naming the voice | `200`, `audio/wav`, 44.1 kHz mono, 3.02 s |
| delete | `200` `{"success":true,"message":"Voice 'Narrator_01' deleted successfully"}` |
| speech naming the deleted voice | `400` `unknown voice` (proof the delete took effect) |

The delete response is also how you can confirm which model handled the call: its
`_hidden_params` names the model group, and the speech probe above is the
authoritative check that the registry actually lost the voice.

The list response for Drama 3 itself carries `voices`, `uploaded_voices`, and
`cache_stats`; parse those rather than the OpenAI-shaped profile contract. See the
caveat in [Gateway endpoints](#gateway-endpoints) about reaching that listing with
a GET.

Upload constraints (enforced by the pod):

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

Any other unregistered `voice` name reaches the pod and is rejected:

```json
{"error":{"message":"litellm.BadRequestError: Drama3Exception - {\"error\":{\"message\":\"Drama 3 request: unknown voice 'alloy'; use default or an uploaded voice\",\"type\":\"BadRequestError\",\"param\":null,\"code\":400}} ..."}}
```

Omitting `voice` entirely is fine; the request synthesizes with the default voice.

---

## 7. Sampling parameters and limits

| Parameter | Default | Notes |
| --------- | ------- | ----- |
| `temperature` | 1.0 | Forwarded |
| `top_p` | 0.9 | Forwarded |
| `top_k` | no cap on the main token | The codebook heads cap at 30 regardless, because wider caps cost speaker similarity. A main-token cap of 30 makes a stream fall silent after its first phrase |
| `max_new_tokens` | model default | Forwarded |
| `repetition_penalty` | 1.0 | Forwarded. **Must be 1.0**; anything else is a 400 |
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

Works through the gateway. Requires `response_format: "pcm"`; a `wav` container
states a total length and cannot be cut into chunks, and the pod rejects the
combination outright. `stream_format` selects the envelope:

```bash
# raw PCM passthrough (default)
curl -sN -X POST "https://litellm.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"fishaudio/drama-3","input":"...","stream":true,"response_format":"pcm"}' \
  -o out.pcm

# SSE events
curl -sN -X POST "https://litellm.ecouncil.ae/v1/audio/speech" \
  -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"fishaudio/drama-3","input":"...","stream":true,"stream_format":"sse","response_format":"pcm"}'
```

SSE events are `{"type": "speech.audio.delta", "audio": "<base64>"}` terminated by
a `speech.audio.done` event.

Beyond `/v1/audio/speech`, the pod also exposes `POST /v1/audio/speech/batch` for
multiple inputs in one request (verified working pod-direct, max 32 items by
default) and a stateful `WS /v1/audio/speech/stream`. Neither is proxied by the
gateway (`POST /v1/audio/speech/batch` through the gateway is a 404), so batch
remains pod-direct.

---

## 8. Error contracts (verified live)

All rows below were reproduced against the production gateway with a
`key_type=llm_api` virtual key. Error text is the pod's own, wrapped in
`Drama3Exception`.

| Case | HTTP | Body |
| ---- | ---- | ---- |
| Missing or invalid gateway key | 401 | `Authentication Error, No api key passed in.` |
| Key without the route in its `allowed_routes` | 403 | `Virtual key is not allowed to call this route. ...` |
| `model` omitted | 400 | `{"detail":{"error":"/audio/speech: Invalid model name passed in model=. Call \`/v1/models\` to view available models for your key."}}` |
| `voice` unregistered (e.g. `alloy` on Drama 3) | 400 | `Drama 3 request: unknown voice 'alloy'; use default or an uploaded voice` |
| `input` is an object rather than a string | 400 | `input must be a string` |
| `repetition_penalty` other than 1.0 | 400 | `Drama 3 request: repetition_penalty must be 1.0; got 1.5` |
| `instructions` set | 400 | `instructions is not supported by this model` |
| `seed` set | 400 | `seed is not supported by this model` |
| `language` set | 400 | `language is not supported by this model` |
| Unknown field (e.g. `bogus_field`) | 400 | `unknown speech request field: bogus_field` |
| `stream: true` without `response_format: "pcm"` | 400 | `Drama 3 request: stream=true requires response_format='pcm'` |
| Unresolvable `references` URL | 400 | `Could not resolve media URL hostname: example.invalid` (`param: references.audio_path`) |
| Reference longer than 60 s | 400 | `references.N: reference audio lasts X s, longer than the 60 s limit` |
| Speaker tag other than 0 | 400 | `Drama 3 request: only speaker 0 is supported; got speaker tags [...]` |
| Upload without the audio file | 400 | `'ref_audio' is required for Drama 3 voice registration. Provide the reference audio file.` |
| Upload with `audio_sample` instead of `ref_audio` | 400 | same as above (the gateway reads `ref_audio`) |
| `POST /v1/audio/speech/batch` | 404 | `{"detail":"Not Found"}` (route not proxied) |
| `GET /v1/audio/voices` | 405 | `{"detail":"Method Not Allowed"}` (use `GET /v1/voices`) |
| `GET /v1/audio/health`, `/v1/audio/metrics` | 403 | ops-only routes, not on the `llm_api` allowlist |
| `DELETE /v1/audio/voices/{name}` | 404 | `{"detail":"Not Found"}` (use `DELETE /v1/voices/profiles/{name}`) |

Per-field validation errors pass through with the pod's original message intact
inside `error.message`, wrapped in `Drama3Exception`. Unlike the diarize route
there is no unwrapping to a spec-shaped body, so read the text inside the wrapper.
The trailing `LiteLLM: model group ... no fallback model group was found` sentence
is router noise appended to every 4xx; the pod's message is the part before it.

Batch (`POST /v1/audio/speech/batch`) is 404 through the gateway. It exists on the
pod but no gateway route proxies it, so batching stays pod-direct (§12).

---

## 9. Mode

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
- `POST /v1/chat/completions` with this model no longer reaches the pod: the
  gateway's chat path resolves the `drama3` provider, which has no chat config,
  and returns `Unmapped LLM provider for this endpoint. You passed
  model=fishaudio/drama-3, custom_llm_provider=drama3`. Use the speech route.
- Anything that groups models by declared mode now classifies this model as a
  speech model.

---

## 10. Performance

Measured on the Al Ain H200 through the gateway, single request, serial:

| Input | Output audio | Wall time | Notes |
| ----- | ------------ | --------- | ----- |
| 25 chars (`Hello, how are you today?`) | 1.67-2.18 s | 0.72-0.84 s | Three runs; length varies per generation |
| 52 chars (`speed: 1.0`) | 3.30 s | ~1.1 s | |
| Same at `speed: 2.0` | 1.53 s | ~1.1 s | |
| 1,220 chars | 95.11 s | 15.1 s | ~6.3x faster than realtime |

Generation length varies between identical requests, so compare durations as ranges rather than exact values. The pod's own benchmark notes (single H200, default placement) report 88.9 ms TTFA at concurrency 1 and 927 frames/s at concurrency 16. Non-streaming requests through the gateway return all audio at once once generation finishes; use `stream: true` with `response_format: "pcm"` (§7) when time to first audio matters.

---

## 11. Route/field notes for clients

- `model: "fishaudio/drama-3"` is mandatory on every route that reads a body; omission is not defaulted.
- `voice` is optional and defaults to the default voice. Register a voice first (§6) to use a custom one; unregistered names are rejected by the pod.
- Read the sample rate from the WAV header; do not assume 24 kHz.
- Direction tags (`[laughs]`, `<whisper>`, `Direction:`/`Line:`) work through the gateway because they ride inside `input` (§5).
- `stream: true` works but needs `response_format: "pcm"`; the pod rejects it with a container format. Judge streaming by flat TTFB, not by a `chunked` header.
- `references` clones inline (§6). With no cluster egress, prefer inline base64 `data` over a URL, or upload the voice once.
- `input` must be a string. Putting references or takes inside it fails with `input must be a string`.
- Sampling controls (`temperature`, `top_k`, `top_p`, `max_new_tokens`) are forwarded; `repetition_penalty` must stay 1.0.
- Unknown fields fail with `unknown speech request field`, so there is no silent dropping to rely on or be surprised by.
- Voice upload needs **both** `ref_audio` (the file field) and `model=fishaudio/drama-3` (a form field). Miss `model` and the voice lands in another provider's registry.
- `GET /v1/voices` cannot be scoped with `?model=`; the gateway ignores the query string, so it answers with whichever voice-capable deployment the router picks first. Use a body-bearing call to target Drama 3 (§6).
- Batch (`POST /v1/audio/speech/batch`) is 404 through the gateway; it stays pod-direct (§12).
- Spend logging: like the other audio-slice routes, this path does not write LiteLLM spend rows, so usage tracking for TTS is not available yet.

---

## 12. Pod-direct access

Useful for the two things the gateway does not proxy: batch synthesis and the
`GET /v1/audio/voices` listing that the gateway cannot model-scope.

```bash
# find the Drama 3 pod (name is the OICM deployment uuid)
kubectl -n adeo get pods | grep -i drama

# forward its HTTP port
kubectl -n adeo port-forward deploy/<drama3-deployment> 8080:8080

# now the pod's own surface is directly reachable
curl -s http://localhost:8080/v1/audio/voices
curl -s -X POST http://localhost:8080/v1/audio/speech/batch \
  -H "Content-Type: application/json" \
  -d '{"items":[{"input":"one","voice":"default"},{"input":"two","voice":"default"}]}'
```

The pod requires no authentication, so a port-forward or an in-cluster client is
enough. Requests sent this way bypass routing, auth, guardrails, and spend
logging; use it for debugging and for the two routes above, not as a production
path.

### Deployment-side reference

The serving-side tuning doc (flags, concurrency tables, int4 checkpoint, sampling defaults, hard constraints such as the mandatory `triton` attention backend) lives outside this repo:

`mlops/oicm-custom-model-server-guide/docs/sglang/fishaudio-drama3.md`

One of its claims is worth knowing before you debug a request through the gateway:

- Its `stream: true` examples require `response_format: "pcm"`. That requirement holds through the gateway too: the pod enforces it and the field is forwarded, so a stream request with `wav` fails on both sides rather than being silently downgraded.

The authoritative upstream sources are the SGLang-Omni cookbook
(`docs/cookbook/fishaudio_drama3.md` in `sglang-omni`, pinned at tag `v0.2.0`,
the version this pod runs) and
`sglang_omni/models/fishaudio_drama3/speech_request.py`, which holds the exact
accepted and rejected field list this guide's §4 and §7 are derived from.
