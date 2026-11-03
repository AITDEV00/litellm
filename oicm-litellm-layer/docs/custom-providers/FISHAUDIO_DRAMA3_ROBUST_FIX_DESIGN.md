# Drama 3 registration: a robust fix, and the patterns behind it

> **Superseded for the registration half by [CAPABILITY_REGISTRATION_BY_PROBE.md](CAPABILITY_REGISTRATION_BY_PROBE.md).** That document replaces the mode-detection ideas here (Patterns 2 and 3) with behavior probing, after verification showed path shape cannot distinguish VibeVoice-ASR (correctly `chat`) from Drama 3 (incorrectly `chat`). Pattern 1, the `hosted_vllm` TTS pass-through config, still stands and is now independent.

Follow-up to [FISHAUDIO_DRAMA3_REGISTRATION_DIAGNOSIS.md](FISHAUDIO_DRAMA3_REGISTRATION_DIAGNOSIS.md). That document correctly identified the two defects but proposed detection heuristics as the fix. This one replaces those proposals with an established-pattern design, after live verification falsified part of the earlier plan.

## Why the earlier plan was not robust

The diagnosis proposed a "serve-intent probe" reading `supported_task_types` from `/v1/models`. Verified live: this pod publishes **no** `supported_task_types`, and its `/model_info` carries engine counters only (request_backlog, tp_size, etc), no task metadata. A probe cannot read what the server does not publish. Path-shape heuristics were never going to be robust either: VibeVoice-ASR registers as `chat` **correctly**, because it is an ASR model served through the chat surface, while Qwen3-ASR registers as `audio_transcription` via the native surface. A path cannot tell those apart; only the deployment's declared intent can.

## The patterns, and where each one applies

### Pattern 1 — Pass-through proxy (the openai route out of Drama 3's field drop)

The field drop is the classic gateway anti-pattern: the proxy understands too much, so any backend field it doesn't know about silently vanishes. Established remedy, used verbatim by AWS API Gateway's proxy integrations: the proxy forwards the request body uninterpreted and lets the authoritative server validate it ("adding a field needs no gateway change"). Postel's law: be conservative in what the proxy adds, liberal in what it forwards, and let the endpoint that owns the schema do the rejecting.

Applied here, minimal form: a per-modality pass-through TTS config for `hosted_vllm`, because sglang/vLLM-family serving stacks already speak the OpenAI speech shape natively, so any OpenAI-shaped TTS pod gets correct behavior without a per-checkpoint provider entry. Concretely:

- `litellm/llms/hosted_vllm/text_to_speech/transformation.py` — `HostedVLLMTextToSpeechConfig(BaseTextToSpeechConfig)`
- `transform_text_to_speech_request` returns `dict_body` built from `model` + `input` + every non-internal kwarg (the `_collect_passthrough` shape already exists in-house in `litellm/llms/inception/text_to_speech/transformation.py:27` and `litellm/llms/omnivoice/common_utils.py:69`; hoist it to `litellm/litellm_core_utils/llm_request_utils.py` next to the existing `serialize_multipart_form_fields` rather than copying it a third time)
- the entrypoint condition in `litellm/main.py:8396` — `custom_llm_provider in litellm.openai_compatible_providers and text_to_speech_provider_config is None` — must narrow to exclude hosted_vllm once its config exists, otherwise the generic branch is never reached
- the final dispatch tuple at `litellm/main.py:8776` — the hardcoded `("hamsa", "inception", "omnivoice")` allowlist — must gain `"hosted_vllm"` (and should be replaced by a membership test on a registry, not a literal tuple, so the next provider doesn't need a `main.py` edit)

End-to-end streaming is already plumbed and requires no new code, verified: the generic handler posts `dict_body` with `stream=request_data["dict_body"].get("stream") is True` (`llm_http_handler.py:12296`), the pod returns chunked PCM, `HttpxBinaryResponseContent.aiter_bytes` delegates to httpx's streaming iterator, and the proxy route already wraps the response in `StreamingResponse(_audio_speech_chunk_generator(response), ...)` (`proxy_server.py:13061`). A client's `stream: true` + `pcm` flows through untouched. No new proxy code needed for streaming delivery.

The `voice` requirement disappears as a side effect, because the config path replaces the OpenAI branch that imposed it.

What pass-through does not fix: the "unknown speech request field" errors the pod itself returns. That is the correct division of labor — the pod owns its schema, the proxy forwards, and a client sending `style` gets a loud 400 from the authority, not a silent 200 from the middlebox.

### Pattern 2 — Declarative reconciliation (declared intent beats inferred shape)

Kubernetes operator pattern: the controller's job is to reconcile observed state toward **declared** state, and where intent is ambiguous, the declaration wins, with detection as the fallback. The strongest form of declaration here already exists in the codebase's own conventions: the per-deployment ConfigMap (`configmap-{uuid}-main`), which the controller already reads for `MODEL_ID` and `EXTRA_ARGS` (`local_deployments.py:122,173`).

Add `MODE` as a read ConfigMap field with a documented enum:

```
MODE=audio_speech   # or chat, audio_transcription, diarization, ...
```

Detection order becomes: ConfigMap `MODE` override wins; otherwise fall back to today's `detect_mode_from_paths`. This is the same override philosophy as `HEALTH_CHECK_MODELS`-style knobs, and matches the operator best practice of "prefer what the workload declares over what a controller can infer".

Why not add `supported_task_types` publishing to the pod? It would be the *most* honest signal (the serving software computing its own capabilities), but it requires an sglang-omni upstream change, crossing a repo boundary this fix should not take on. The ConfigMap field is the in-boundary equivalent: a human declares intent once per deployment, at deploy time, which is when the human knows the intent.

### Pattern 3 — Drain the inferred-path swamp (mode detection ordering)

Declarative override first, but the fallback detection still exists, so order it by **usefulness**, not by historical accident. The current `_PATH_MODES` table maps paths to modes in the order they were added, so `TRANSCRIPTION_PATH` (added for Qwen3-ASR) precedes `TTS_PATH` (added later), and a pod exposing both resolves to `audio_transcription`. VibeVoice proves the reverse pair is equally real. The table should be ordered by the mode a client can actually call:

1. TTS path present (and chat cannot usefully serve it) -> `text_to_speech`
2. Transcription path present (and chat cannot usefully serve it) -> `audio_transcription`
3. Diarize, OCR, embedding, rerank — unchanged
4. Chat remains the default last resort

With the ConfigMap override as the primary mechanism and this table as fallback, the diarize-before-transcription comment (`controller/models.py:188`) becomes a historical note rather than load-bearing ordering.

### Pattern 4 — Reconcile drift, do not invent a new source of truth

A design where the controller stamps `mode` from ConfigMap needs a drift rule or it creates a second source of truth: someone edits the ConfigMap, the next full sync stamps the new mode into the row, and nothing tells the operator the row drifted from the declaration. Operator practice: the reconciler PATCHes observed state toward declared state, and logs the transition. The controller already has this exact shape for model registration itself — `_pick_richest_entry` dedupes rows each 300s sync. Extend the same discipline to `mode`: the registered row's `model_info.mode` follows the ConfigMap on every full sync, with a one-line log when it changes.

## What this deliberately does not do

- **No new `fishaudio` provider.** The checkpoint name is not the serving contract; `hosted_vllm` is. Drama 3 is an OpenAI-speech-shaped pod behind hosted_vllm, exactly like its sglang/vLLM siblings. A per-checkpoint provider would need one entry per future TTS checkpoint and teaches the controller nothing general.
- **No capability probing of the chat endpoint.** Sending synthetic traffic to a pod to guess its mode adds load, adds failure modes, and still guesses. The ConfigMap declaration is a human's answer, not an inference.
- **No touching the chat-first protection for VibeVoice-class pods.** The chat-first rule protects a real case; the ConfigMap override is what handles Drama 3-class pods, so the heuristic never has to be weakened.
- **No upstream (sglang-omni) changes.** Out of scope for a gateway-side fix; `supported_task_types` publishing is noted as the ideal future signal.

## Fix sequence

1. **`hosted_vllm` pass-through TTS config** (Pattern 1). Self-contained, restores `stream` + `references` + removes the `voice` requirement for every OpenAI-shaped TTS pod, and the streaming path is already fully plumbed. Test by replaying the §3 probe pairs from the usage guide: `stream: true` + `wav` must 400, invalid `references` must 400, and a streaming PCM request must arrive chunked.
2. **ConfigMap `MODE` + controller stamping** (Patterns 2 + 4). Fixes the mislabeled `chat` mode for existing deployments (including Drama 3) without touching detection.
3. **`_PATH_MODES` reorder** (Pattern 3). The residual ordering bug, ordered by usefulness once the override exists.

Each step is independently shippable and independently revertible, which is the property that makes the sequence robust rather than merely correct.
