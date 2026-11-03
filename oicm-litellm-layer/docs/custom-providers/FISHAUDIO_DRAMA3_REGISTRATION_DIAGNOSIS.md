# Drama 3 registration: why the mode is wrong and why fields are dropped

> **Status (2026-11-03): Defect 1 is FIXED and deployed.** Both controllers run
> `oicm-discovery-controller:0.1.0-20261103-4d922d8`, and `fishaudio/drama-3`
> registers as `mode: audio_speech` with `oicm_task: Text-to-Speech` and
> `health_check_voice: default` on prod and dev. The fix was **not** the
> chat-precedence change explored below: it came from OICM's declared
> `inference_task`, used as tier 0 with a mounted-surface gate. See
> [CAPABILITY_REGISTRATION_REVIEW2.md](CAPABILITY_REGISTRATION_REVIEW2.md#shipped-state-2026-11-03).
> Defect 2 (dropped request fields) is still open on the gateway.

Two independent defects surfaced while writing the [Drama 3 usage guide](../usage-guides/FISHAUDIO_DRAMA3_TTS_GUIDE.md). They have different root causes and need different fixes, so they are recorded separately here.

The short version: `openapi.json` **is** read correctly. The controller's problem is the order in which it consults the paths it reads. The dropped fields are a separate, gateway-side problem that no amount of controller work will fix.

---

## Defect 1: mode is `chat` because chat is checked first

### Evidence

Run against the live pod's actual `openapi.json`:

```
CHAT_PATH present : True
TTS_PATH present  : True
detected mode     : chat -> litellm: chat
detected provider : hosted_vllm
```

`detect_mode_from_paths` in `controller/models.py:211` short-circuits at line 229:

```python
if CHAT_PATH in paths:
    return "chat"
```

The pod exposes `/v1/chat/completions` because sglang-omni serves the whole omni surface, so chat wins and `/v1/audio/speech` is never considered. The probe itself is fine; only the precedence is wrong for this class of pod.

### Second, latent bug in the same table

Even if the chat short-circuit were removed, the mode would still be wrong. Feed the same paths through the table:

```
what the TTS branch WOULD return if chat did not win:
    audio_transcription
```

`_PATH_MODES` (`controller/models.py:185`) lists `TRANSCRIPTION_PATH` **before** `TTS_PATH`, and the table returns its first hit. This pod exposes both `/v1/audio/transcriptions` and `/v1/audio/speech`, so it resolves to `audio_transcription`.

This is the same class of bug the existing comment already documents for diarize versus transcription ("Diarize before transcription: a coro pod serves both surfaces"). That fix ordered one pair correctly and left the next pair exposed. Any pod that serves transcriptions *and* TTS hits it.

### Consequence

The model registers as `mode: chat`, so:

- `POST /v1/chat/completions` reaches the pod and fails with `Drama 3 request: the speech input must be text or an object with text and references`.
- `POST /v1/audio/speech` still works, because routing resolves by model name and the registered `api_base` already ends in `/v1`.
- Anything grouping models by declared mode (dashboards, capability filters, auto-routers) mislabels the model.

### Proposed fix

Not yet applied; this section is the design.

1. **Reorder the table so speech precedes transcription.** Put `TTS_PATH` (and `HAMSA_TTS_PATH`) before `TRANSCRIPTION_PATH`. A pod exposing a speech endpoint is a speech model, for the same reason the diarize comment gives. Ordering by specificity rather than by historical accident is the general rule; `_PATH_MODES` should say so in a comment rather than rely on each pair being fixed when it bites.

2. **Stop letting chat unconditionally win.** The current rule is deliberately conservative, and the docstring explains why: a chat server that also exposes a transcription endpoint must not be registered as an audio model, or every request 404s. The rule that preserves that intent while covering this pod is to check whether the chat path is actually *usable*. Two options:

   - **Path-ratio heuristic (cheap, no extra request).** If the pod exposes chat plus exactly one audio surface and no streaming responses, prefer the audio mode. Fragile, because it infers intent from counts.
   - **Serve-intent probe (accurate, one extra request).** Probe the pod's `/v1/models` for `supported_task_types`, which the controller already reads in `_discover_model_ids`. If the pod advertises speech only, register `text_to_speech` even when `/v1/chat/completions` exists. This is the option that matches what the pod actually declares rather than guessing from path shapes.

   The probe-based option is preferable, and `_probe_capability_facts` already establishes the pattern for asking a pod what it can do.

3. **Add a regression test.** `controller/models.py` has no test that a speech-only pod listing chat is classified as speech. The existing controller tests cover exclusions and deploy parity, not mode precedence, which is why this shipped.

### Is a new provider needed for the mode fix?

No. Mode and provider are independent axes here, and the mode fix is entirely about path precedence. A provider entry would not change `detect_mode_from_paths`.

---

## Defect 2: request fields are dropped by the gateway, not the controller

### Evidence

The same request, pod-direct versus through the gateway, using values the pod rejects outright:

| Request | Pod direct | Gateway |
| ------- | ---------- | ------- |
| `stream: true` with `response_format: "wav"` | `400 stream=true requires response_format='pcm'` | `200 audio/wav` |
| `references` → `https://example.invalid/nope.wav` | `400 Could not resolve media URL hostname` | `200 audio/wav` |

The pod rejects both. The gateway returns audio, so the fields never arrive.

### Cause

`detect_provider` returns `hosted_vllm`, and `get_provider_text_to_speech_config` has no branch for it, so it returns `None`. `litellm/main.py:8397` then takes its OpenAI TTS branch:

```python
elif custom_llm_provider == "openai" or (
    custom_llm_provider in litellm.openai_compatible_providers
    and custom_llm_provider not in AZURE_OPENAI_AUDIO_PROVIDERS
    and text_to_speech_provider_config is None
):
```

That branch requires a string `voice` (hence the `voice is required` error on an otherwise valid Drama 3 request) and builds the upstream call from a fixed parameter set: `model`, `input`, `voice`, `response_format`, `speed`, `instructions`. The rest of `**kwargs` goes to logging. So `references`, `stream`, and any future model-specific field are dropped while the response looks perfectly valid.

### Would a Fish Audio provider fix this?

Yes, and this is the case where a provider is the right answer. A provider config would:

1. Replace the OpenAI branch, removing the spurious `voice` requirement.
2. Supply `transform_text_to_speech_request`, which returns a `dict_body` built verbatim, so `references` and `stream` reach the pod unchanged.
3. Let `get_supported_openai_params` declare exactly what the pod accepts, so genuinely unsupported fields fail loudly instead of being silently discarded.

The base interface already supports all three; nothing needs inventing. `BaseTextToSpeechConfig.transform_text_to_speech_request` returns a `TextToSpeechRequestData` whose `dict_body` is forwarded as-is.

Registration would mirror the existing OICM slices:

- `litellm/llms/fishaudio/{text_to_speech/transformation.py, common_utils.py}`
- a `FISHAUDIO` member on `litellm.LlmProviders`
- a branch in `get_provider_text_to_speech_config` in `litellm/llms/oicm_providers/registry.py`
- `"fishaudio"` added to `KNOWN_PROVIDERS` in `controller/models.py`

One wrinkle worth deciding before implementation: `detect_provider` matches on a substring of `owned_by` or the model id. Here `owned_by` is `sglang-omni` and the model id is `fishaudio/drama-3`, so a `"fishaudio"` token would match the id. That works, but it means the provider is inferred from the checkpoint name rather than the serving software, which is exactly the ambiguity the `TODO(future)` in `detect_provider` already calls out. It also means any other sglang-omni-served TTS model needs its own entry rather than inheriting one.

---

## Recommendation

**Superseded.** The ordered recommendation below was the first pass; [FISHAUDIO_DRAMA3_ROBUST_FIX_DESIGN.md](FISHAUDIO_DRAMA3_ROBUST_FIX_DESIGN.md) replaces it after live verification falsified parts of it (the pod publishes no `supported_task_types`, so the serve-intent probe had no data source, and VibeVoice-ASR shows the chat-first rule protects a real case). Kept for the record of what was considered.

Land them in this order:

1. **Provider slice for fishaudio (gateway).** This is what restores streaming and voice cloning, which are the features people actually want from Drama 3. It also removes the `voice` requirement, which is the first thing every caller trips over.
2. **`_PATH_MODES` reorder (controller).** Small, self-contained, and fixes the mislabel for every pod that serves both speech and transcription.
3. **Chat-precedence fix (controller).** Needs the serve-intent probe and a careful test, because the current rule exists to prevent a real 404 failure mode. Do not weaken it without covering the transcription-with-chat case it protects. *(Verification note: this probe cannot be built today; this pod publishes no `supported_task_types`. The robust-fix design replaces this step with a declared ConfigMap `MODE` override.)*

Until step 1 lands, the usage guide documents the current behaviour accurately, so callers are not surprised by a 200 with the wrong audio.
