# NVIDIA Nemotron Transcribe: Gateway Usage Guide

> **Scope**: Live, tested usage of **NVIDIA-Nemotron-3.5-Transcribe-30B-A4B**
> through the LiteLLM gateway, including the differences between native NIM and
> gateway behavior.
>
> **Verified**: 2026-11-17/18 against the production gateway and the single
> available NIM deployment. This is an audio **transcription** model; it is not a
> text-generation, text-to-speech, or microphone-realtime transcription service.

## Connection and model

| Setting | Value |
|---|---|
| Gateway base URL | `https://litellm.ecouncil.ae` |
| Primary API | `POST /v1/audio/transcriptions` |
| Audio-in-chat alternative | `POST /v1/chat/completions` |
| Model ID | `nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B` |
| Inference protocol | Multipart audio file, or base64 WAV in chat JSON |
| TLS | Publicly trusted DigiCert certificate; do **not** disable verification |

Set the credential locally. Use an inference-capable scoped key with
`allowed_routes=["llm_api_routes"]` (or a narrower literal route permission).
**Never put your key in the request URL, a log, or a checked-in file.**

```bash
export LITELLM_API_KEY='<your-litellm-api-key>'
export NEMOTRON_MODEL='nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B'
export NEMOTRON_AUDIO='/path/to/audio.wav'
```

Confirm model registration through list, not a per-model-id lookup:

```bash
curl -sS https://litellm.ecouncil.ae/v1/models \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  | jq '.data[] | select(.id == env.NEMOTRON_MODEL)'
```

`GET /health/liveness` and `GET /health/readiness` are **proxy** health checks,
not native NIM routes. The tested NIM `GET /health` is not available through
this gateway; requests for it with an inference-only key receive 403.

The tested model is registered with
`model_info.mode=audio_transcription` and `drop_params=true`. The gateway uses
the NIM's public OICM model name and maps it to the lowercase internal NIM
model name. Callers should send the full model ID as listed above.

## Audio support and transcription languages

The model accepts a complete audio file, not live PCM microphone input. A known
working source is mono, 16-bit, 16 kHz PCM WAV (10.885 s). Other supported NIM
container formats should be established against the model's own documentation
and tested on your actual audio; they were not exhaustively tested here.

The native model metadata advertises these transcription-language codes:

- `en`, `ar`, `zh`, `fr`, `de`, `hi`, `it`, `ja`, `es`, `ko`, `pt`, `ru`, `th`,
  `hy`, `nn`, `bg`, `hr`, `cs`, `da`, `nl`, `et`, `fi`, `el`, `he`, `hu`, `id`,
  `lt`, `lv`, `mt`, `pl`, `ro`, `sk`, `sl`, `sv`, `uk`, `vi`;
- and `fr-CA`, `en-GB`, `en-US`, `zh-CN`, `zh-TW`, `pt-BR`.

The native model also advertises translation targets `en`, `am`, `ar`, `az`,
`bg`, `ca`, `cs`, `da`, `de`, `el`, `et`, `fa`, `fi`, `fil`, `fr`, `hi`, `hr`,
`hu`, `id`, `it`, `ja`, `ka`, `ko`, `lt`, `mr`, `nl`, `nn`, `pl`, `pt`, `ro`,
`ru`, `sk`, `sl`, `sv`, `te`, `uk`, `vi`, `zh`.

Omitting `language` lets the NIM detect the spoken language during ordinary
transcription. Use a valid BCP 47 tag when supplying it. Language detection and
explicit `language=en` were verified on the English sample, not on a
multilingual, format-coverage, or WER corpus. Explicit `language` is required
for the timestamp feature described below.

## 1. Basic transcription (recommended)

```bash
curl -sS -X POST https://litellm.ecouncil.ae/v1/audio/transcriptions \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -F "model=$NEMOTRON_MODEL" \
  -F "file=@$NEMOTRON_AUDIO" \
  -F "response_format=json" \
  -F "language=en"
```

Set `response_format=json` explicitly: that is the gateway's safe default.
Direct NIM calls produce `{text, usage}` for omission and for `json`, but
LiteLLM's transformer internally asks the NIM for `verbose_json` when an
OpenAI client leaves the format omitted and then normalizes the result.
`stream` is **omitted** deliberately (see streaming limitations).

Successful default response:

```json
{
  "text": "Because you were sleeping instead of conquering, the lovely rose princess has become a fiddle without a bow, while poor Shaggy sits there a cooing dove.",
  "usage": {"seconds": 10.885, "type": "duration"}
}
```

Always verify `text` is a non-empty string as well as checking HTTP 200. In
this deployment `usage.seconds` is the audio duration, **not** an audio token
count. The transcription model and its usage accounting should not be
represented as a conventional text-token usage budget.

## 2. Verbose JSON and timestamps

Require `response_format=verbose_json` for timing metadata. Supply an explicit
language; the timestamp path requires a recognized BCP 47 tag.

```bash
curl -sS -X POST https://litellm.ecouncil.ae/v1/audio/transcriptions \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -F "model=$NEMOTRON_MODEL" \
  -F "file=@$NEMOTRON_AUDIO" \
  -F "response_format=verbose_json" \
  -F "language=en" \
  -F "timestamp_granularities[]=word"
```

This returned 26 timed words for the test recording, `language=en`,
`task=transcribe`, `duration=10.885`, and CTC word-alignment metadata. Each
word has `word`, `start`, `end`, and `word_index`. Depending on the requested
options, the response can also contain the normalized word `speaker` field and
speaker confidence/role labels.

Use `word`, `segment`, or two separate `timestamp_granularities[]` fields for
`word` and `segment`. The NIM-specific `word_timestamps=true` extension also
worked through the gateway. Prefer the OpenAI-style timestamp field for portable
OpenAI-compatible clients.

**Important omissions:** sending `timestamp_granularities[]` without
`verbose_json` fails (400). Native `diarization=true` requires verbose JSON.
That field was accepted by NIM and returned 200 through the gateway, but its
speaker-timing details were lost in gateway response normalization. In contrast,
the word/segment granularities and `word_timestamps=true` tests **preserved**
`speaker_word_timestamps` and `diarization_timestamps`. Vendor extensions
outside the OpenAI transcription contract can also be silently dropped upstream
by parameter transformation/`drop_params=true`. When `diarization=true` is
explicit, native sections for
`speaker_word_timestamps` and `diarization_timestamps` were absent after gateway response normalization,
while the basic `words` and `segments` fields remained. **Do not use this
gateway transcription surface as your speaker-diarization API.** Use the
separately documented [Pyannote service](PYANNOTE_DIARIZATION_GUIDE.md) when
structured diarization is needed.

## 3. Audio-in-chat and working SSE streaming

The gateway accepted an inline base64 WAV as an `input_audio` message content
part on `/v1/chat/completions`. Its response was a standard chat completion
whose transcript is at `choices[0].message.content`, plus a native
`transcription` metadata object and audio usage. Unlike multipart text
transcription, this path preserved `usage.completion_tokens`,
`usage.prompt_tokens`, `usage.prompt_tokens_details.audio_tokens`, and
`transcription`.

Example using standard Python only:

```python
import base64, json, os, urllib.request

with open(os.environ["NEMOTRON_AUDIO"], "rb") as f:
    audio = base64.b64encode(f.read()).decode()

payload = {
    "model": os.environ["NEMOTRON_MODEL"],
    "messages": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}},
        {"type": "text", "text": "Transcribe the audio."},
    ]}],
    "stream": True,
    "stream_options": {"include_usage": True},
}
request = urllib.request.Request(
    "https://litellm.ecouncil.ae/v1/chat/completions",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json",
             "Authorization": "Bearer " + os.environ["LITELLM_API_KEY"]},
)
text_parts = []
done = False
with urllib.request.urlopen(request, timeout=180) as response:
    for line in response:
        if not line.startswith(b"data: "):
            continue
        data = line[6:].strip()
        if data == b"[DONE]":
            done = True
            continue
        payload = json.loads(data)
        for choice in payload.get("choices", []):
            fragment = choice.get("delta", {}).get("content")
            if fragment:
                text_parts.append(fragment)
                print(fragment, end="", flush=True)
print("\nFinal text:", "".join(text_parts))
```

Use `choices[0].delta.content`, not `choices[0].message.content`, on the SSE
path. The stream ends with a `data: [DONE]` marker. The reusable
`transcribe_chat_stream.py` example checks that marker and rejects an empty
reconstructed transcript. With `stream_options.include_usage=true`, parse
usage-bearing JSON events as they arrive; usage is absent from content-delta
events. The equivalent regular chat response had 158 prompt tokens and 35
completion tokens for a 10.885-second sample. Keep token and audio-seconds
usage forms distinct when accounting.

**This is offline-file audio streamed back as text. It is not a microphone or
streaming-input WebSocket service.** Native NIM can serve streamed text over
`POST /v1/audio/transcriptions` with `stream=true`; through the tested gateway
that request returned a buffered final JSON response instead. That native form
is therefore **not parity-exposed through this gateway**.

## 4. What is and is not supported through this gateway

“NIM mounted the route” is not the same as “the gateway supports the operation.”
The following matrix records response shapes as well as HTTP success codes.
Tested on the reference WAV, which is not a complete audio-format/accuracy
coverage suite.

| Capability / path | Direct NIM observed | LiteLLM gateway observed |
|---|---|---|
| `GET /v1/models` | Lists the NIM | Lists registered gateway models; find the full ID with a filter |
| `POST /v1/audio/transcriptions` (default/json) | 200 + `{text, usage}` | **200 + matching non-empty text** |
| `language`, `temperature` | Supported conventions | **Forwarded successfully** |
| `verbose_json` | Detailed text and timing envelope | **Forwarded and normalized** |
| `timestamp_granularities[]=word` + verbose JSON | Word timings | **26 timed words verified** |
| `timestamp_granularities[]=segment` + verbose JSON | Segment timestamp metadata fields | **Received 200; `segments` was an empty array** in this clip's tested behavior |
| Both granularities | Word/segment timing support | **Received 200**, with native metadata normalization |
| `word_timestamps=true` + verbose JSON | Native word-timestamp extension | **Worked**, preserving the tested native speaker timestamps |
| `diarization=true` + verbose JSON | Native speaker timing structures | **Not parity-supported:** accepted, but native extra sections stripped |
| OpenAI-style `prompt` | Phrase boosting/prompt injection rejected by NIM | **HTTP 400 observed: `invalid_request_error`**, not a supported Whisper-style free-form prompt |
| `response_format=text` | Plain text at HTTP 200 | **200 `text/plain`, body is the transcript, not a JSON envelope** (verified on dev 2026-11-20) |
| `response_format=srt` or `vtt` | Subtitle response at HTTP 200 | **200 `application/x-subrip` / `text/vtt`, true subtitle document bytes** (verified on dev 2026-11-20; cold and cached responses are byte-identical) |
| `stream=true` multipart transcription | SSE text stream | **200 buffered JSON `{text, usage}`**, so no incremental multipart parity |
| `/v1/audio/translations` | Native translation works; with `target_language=en` produces translated text | **200 translated text** (dev 2026-11-20: `target_language=en` returned "Eighty-seven years ago..." for the same clip; cold and cached responses match; works with and without `/v1`) |
| `/v1/audio/translations` without `target_language` | Provider validates the field | **200**: the pod translates to its default language; the gateway forwards truthfully and adds no field |
| `/v1/audio/translations` + `response_format=text` | Plain text at HTTP 200 | **200 `text/plain`, translated body as a true document**, cold and cached byte-identical (dev 2026-11-20) |
| Audio-chat with a transcription instruction | Chat-text transcription is a separate request form | **Not an alternative native translation route**; only transcription was tested |
| `/v1/chat/completions` + base64 `input_audio` | Text/audio response | **200**, with the full public model ID and native `transcription` preserved |
| `/v1/chat/completions` + `stream=true` | Streamed chat | **200 SSE** with `data: ...` deltas, a terminal `[DONE]`, and usage |
| `/v1/responses` + the same audio shape | `asr_invalid_request` | **No supported equivalent; do not use as the documented audio API** |
| `/openai/v1/audio/translations` | Not applicable (native NIM path) | 500 `OpenAIException` accompanied by upstream-provider context, not a supported public translation route |
| `GET /health/liveness` / `/health/readiness` | 404 on native audio listener | **200 proxy health** only |
| `GET /v1/health/ready`, `/v1/health/live` | 200 NIM health | **404 public gateway** |
| `/v1/audio/translations` + multipart `stream=true` | Rejected HTTP 400 in the native test | **400 truthful error**: this pod answered the nonstreaming call with an SSE stream, so the gateway reports `streaming is not supported for this operation. Resend without stream=true` (dev 2026-11-20; the pod streams rather than rejecting, unlike the earlier native test) |
| `GET /v1/models/{encoded-full-model-id}` | Not a required native lookup route | **404**: slashes in the model name are not usable in this path lookup |
| Native `/v1/metrics`, `/v1/license`, `/v1/manifest`, `/v1/metadata`, `/v1/version` | Mounted on native OpenAPI listener | **Not model inference APIs; treated as private operations endpoints, not public Nemotron endpoints** |

**Unsupported requests must not be counted as successes.** In particular, this
registration has `drop_params=true`: fields that the hosted-vLLM transformer
does not recognize may be silently dropped before the NIM sees them. A request
returning HTTP 200 proves only that the surviving request completed. Unknown
fields, explicit translation/diarization options, and streaming/audio metadata
therefore require semantic result inspection, not just status checking.

Native provider recognition of a form field or a JSON request's content is
subject to its own validation. Unsupported language tags, bad language codes,
missing files, empty files, invalid timestamps, or malformed audio can still
produce upstream validation failures; that is not silently fixed by the
gateway's parameter dropping.

## 5. Tested concurrency and performance

The benchmark used **hash-distinct full-duration WAV variants** made from the
NIM's built-in 10.885-second speech sample by rotations and gain changes.
Each request required an HTTP 200 response and a non-empty transcript. Every
result was saved in full after every worker exited. The
client used fresh HTTPS connections and explicitly requested **non-streaming
JSON**, with no implicit retry of failed inference calls.

The offered wall interval includes client transport, upload/ingress, proxy
queuing, upstream inference, and response retrieval. It is **not** a GPU-only
measurement. For $N$ clips of duration $D$ seconds and completed burst wall
time $T$ seconds, the reported aggregate scale is $N D / T$ (audio seconds per
wall second), while `RTF_wall_per_audio_second` is $T/D$. Client-side audio
generation occurs before the synchronized burst and is not in this interval.

| Offered burst / route | Successes | Wall s | p50 s | p95 s | Max s | Aggregate audio s / wall s | Completed req/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| Gateway, 64 | **64/64** | 7.999 | 7.036 | 7.428 | 7.988 | 87.1 | 8.00 |
| Same-harness loopback alias, 32 | **32/32** | 4.136 | 3.498 | 4.072 | 4.125 | 84.2 | 7.74 |
| Same-harness loopback alias, 64 | **64/64** | 5.742 | 3.795 | 5.687 | 5.724 | 121.3 | 11.15 |
| Same-harness loopback alias, 128 (shared-GPU upload pressure) | **64/128** | 11.029 | 10.049 | 10.910 | 11.014 | 63.2 successful | 5.80 successful |
| Gateway, 128 concurrent | **128/128** | 10.003 | 8.514 | 9.887 | 9.976 | 139.3 | 12.80 |
| Gateway, 256 concurrent | **256/256** | 34.026 | 25.850 | 30.049 | 33.980 | 81.9 | 7.52 |
| Gateway, 768 concurrent | **756/768** | 95.040 | 73.808 | 90.763 | 94.663 | 86.6 successful | 7.95 successful |
| Native alias, prior **in-pod** same-boot control, 768 | **768/768** | 4.491 | 3.417 | 4.026 | 4.098 | 1,861.6 | 171.0 |
| Native NIM direct, prior **in-pod** same-boot control, 768 | **768/768** | 5.413 | 4.325 | 5.056 | 5.217 | 1,544.4 | 141.9 |

For the 768 offered gateway inputs, aggregate audio **successfully processed**
was 8,228.68 s relative to 95.04 s of wall-clock, or 86.6× aggregate audio-rate.
Counting all offered audio regardless of 12 failed requests would give 87.9×.
The table purposely uses the successful count and clearly records the failures.

### What the bulk benchmarks do and do not establish

- The gateway completed **every** request at 256 and **756** at 768. It did
  **not** inherit native NIM 768/768 reliability in this public-path test.
- The twelve failures ended as client-visible `RemoteDisconnected: Remote end
  closed connection without response` after roughly **67.18–84.28 s**. They
  were not 200s, were not HTTP 429s, and must not be counted as retry-successes
  or inferred as a fixed 60-second limit. The precise component that closed
  those sockets remains **unproven** from this experiment.
- The gateway p50 rose from 25.85 s to 73.81 s as offered concurrency tripled,
  while successful completions rose only from 7.52 to 7.95/s. For comparison,
  documented native NIM burst capacity is substantially greater.
- Two distinct transport/harness constraints must not be conflated. First, two
  attempted **768** direct external forwarding-channel bursts did not complete
  informatively and are excluded from all tables; they do not contradict the
  earlier in-pod native capacity result. Second, a later, modest **128** direct
  external burst completed only **64/128**, yielding **63 HTTP 429 `ASR upload
  memory budget is full` and one HTTP 503 transcribe-backend-unavailable error**.
  Its interval was 11.029 s, p50 10.049, p95 10.910 s. The same client harness
  through the gateway returned 128/128. At 32 and 64 the direct alias had already
  completed 32/32 and 64/64. This validates the NIM's additional native upload-
  memory pressure under that external burst, but does not replace the established
  in-pod 768/768 result or isolate a universal direct-client limit.
- At matched 64, the direct 64/64 completed faster (5.742 s) than the later
  gateway 64/64 (8.000 s). At 128, the gateway was clean while the later direct
  channel hit native upload-memory pressure. **Do not infer a universal ordering
  or a fixed concurrency ceiling from these individual runs.**
- After the public gateway bursts, NIM was Ready with zero container restarts.
  No deployment was restarted or reconfigured for these measurements.
- All these inputs are transformations of one sample—not independent speakers,
  randomized independent utterances, a multilingual evaluation, or a WER
  accuracy test. The high native burst aggregate is not a sustained throughput
  SLO or a production capacity guarantee.

### Why the gateway path cannot be treated as a streaming direct alias

The installed public gateway path and the custom NIM HTTP alias are materially
different architectures:

1. **Gateway file ingestion materializes the WAV before forwarding it.** The
   transcription route uses `request.form()`, `await file.read()` without a size,
   and then copies those complete bytes into `BytesIO` for the provider call.
2. **The effective production nginx config for the public `/` location has
   `proxy_request_buffering on`** (and default `client_body_buffer_size 8k`)
   when inspected live. The checked-in ingress annotation says
   `proxy-request-buffering: "false"`, but ingress-nginx accepts `on`/`off`
   rather than `false`; this discrepancy needs an operational fix separately.
3. LiteLLM uses non-streaming multipart upstream forwarding for the default
   transcription route and applies its own transcription-response schema
   validation afterward. For the (text, SRT, VTT) formats, this can turn a
   successful native non-JSON response into an HTTP 500.
4. The custom alias instead forwards bounded 64 KiB chunks for uploads and
   responses with no large-request buffering. That is the path supporting the
   prior 768/768 in-pod native admission result.

Related source locations: `litellm/proxy/proxy_server.py` transcription route,
`litellm/proxy/common_utils/http_parsing_utils.py::get_form_data`, and
`litellm/llms/hosted_vllm/transcriptions/transformation.py`. The gateway had
**two production replicas**, each with 16 GiB memory, during the investigated
configuration. No OOM kill or model restart was established by the experiment.
The evidence above demonstrates path asymmetry; it does not by itself identify
the exact socket-closing component, or prove what its per-request limits will
be under unrelated production load.

## Operational recommendations

- **Start at bounded client concurrency** appropriate to the public gateway,
  not NIM's raw 768-lane admission. This experiment first completed cleanly at
  256 but became slow and had 12 disconnects at 768; do not interpret 256 as a
  guaranteed peak, perfect SLA, backlog cap, or gateway-wide rate limit.
- For many files, **offload long bursts from your application**, queue in your
  own bounded workpool, and report HTTP status and empty-transcript outcomes.
- Use **non-streaming JSON** for reliable multipart success. Use chat-audio SSE
  only when you specifically need that supported token-delta form.
- Do not assume non-rejected fields are honored. Check verbose timing shapes,
  language selections, and `transcription`/`usage` fields explicitly.
- **At-least-once retry caution:** an empty/error HTTP response is safer to
  retry with exponential backoff and a cap than a silently accepted late
  duplicate. A retry after socket loss may duplicate an upstream request that
  NIM already accepted. Do not build infinite retry loops; operators should
  enable explicit, honest bounded retries with caller idempotency and budgets.
- Whole-file payload buffering scales approximately with simultaneous clip
  size. Favor direct native streaming through an authorized internal path when
  that is a documented product-access option; **do not attempt unauthenticated
  direct-NIM connections or use its private readiness route as a public API.**

Treat audio and transcription content as subject to your installation's gateway
logging, retention, and privacy policies. This experiment did not establish that
the production deployment logs full audio content; do not assume that logging
excludes it without inspecting policy separately. No request credential or audio
payload is needed in a result file and this guide does not include either.

## Runnable examples and rerun instructions

This tested harness lives under
`oicm-litellm-layer/examples/usage/nemotron-transcribe/`.

- `test_gateway.py`: one reference WAV exercises multipart JSON/verbose JSON,
  word timestamps, chat audio/SSE, model listing, gateway health; the response
  evidence excludes secrets. It was written when the gateway returned 404 for
  `/v1/audio/translations` and 500 for `response_format=text`; those two
  checks now describe the pre-2026-11-20 gateway and need updating before the
  script is reused
- `transcribe_chat_stream.py`: end-to-end streaming chat transcription; it
  reconstructs and prints the full text, verifies, and
  prints the usage event when present.
- `benchmark_gateway.py`: generates bounded unique WAV variants, offers a
  specified concurrent burst, records exact per-request status/error/latency,
  requires non-empty transcripts and writes summary JSON safely. It defaults
  to a 256 cap per invocation, but accepts explicit higher concurrency up to
  its 7,680 request-generation bound. Experimental concurrency is not a
  supported production-level guarantee.

Run with environment variables:

```bash
export LITELLM_API_KEY='<your-litellm-api-key>'
export NEMOTRON_AUDIO='/path/to/reference.wav'

# Functional example:
python3 examples/usage/nemotron-transcribe/test_gateway.py

# End-to-end streaming chat transcription:
python3 examples/usage/nemotron-transcribe/transcribe_chat_stream.py

# Smaller local smoke benchmark:
python3 examples/usage/nemotron-transcribe/benchmark_gateway.py \
  --audio "$NEMOTRON_AUDIO" \
  --concurrency 8 --label smoke --output /tmp/nemotron-burst-smoke.json
```

`benchmark_gateway.py` is Linux/Unix-oriented for its resource accounting,
stdlib-only, and tests non-streaming multipart JSON. Use its `--output` option
to point to private benchmark storage; do not use `/proc` or authenticated
headers as correlation data for saved records. `--no-auth` is intentionally
restricted to loopback endpoints for an operator's private reference controls.
**Re-running 768-way public gateway stress in production requires explicit
operator authorization, careful coordination, and possibly a new scope
because the current key/admin guidance is not a concurrency SLO.**
