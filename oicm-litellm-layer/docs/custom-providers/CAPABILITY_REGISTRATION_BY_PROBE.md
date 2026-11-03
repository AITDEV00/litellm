# Capability registration: probe the deployment, don't trust the route table

> **Demoted to last-resort tier by [CAPABILITY_REGISTRATION_REVIEW2.md](CAPABILITY_REGISTRATION_REVIEW2.md).** Verification found that OICM's `deployment_summary` already carries an `inference_task` declaration (HF-derived enum) that the controller's wire DTO silently drops. Declared task beats probing as the primary tier; the probe table survives as tier 2 for deployments that declare nothing. The three-verdict semantics and method-matched probing below remain correct and are the part worth keeping.

Supersedes the detection proposals in [FISHAUDIO_DRAMA3_ROBUST_FIX_DESIGN.md](FISHAUDIO_DRAMA3_ROBUST_FIX_DESIGN.md). The framing there was wrong: it treated registration as a *path-shape inference* problem and proposed ever-better heuristics. The right framing is that a deployment's capability is a fact you can **ask it for**, and `openapi.json` is a poor proxy for that fact.

Parent direction: the gateway's only job is to serve registered models correctly. OICM and Kubernetes own deployment. So the controller's job is to register each deployment's capabilities *accurately*, not to hardcode `if` conditions that encode one vendor's path list.

---

## Why `openapi.json` is the wrong oracle

It reports **what routes exist**, not **what the deployment can do**. Three verified counterexamples on today's cluster:

| Pod | `/v1/chat/completions` in openapi | Actually usable for chat | Correct mode |
| --- | --- | --- | --- |
| VibeVoice-ASR (vLLM) | yes | **yes** (POST returns 200) | `chat` |
| Drama 3 (sglang-omni) | yes | **no** (POST returns 400, "speech input must be text") | `audio_speech` |
| Qwen3-ASR (vLLM) | — | native transcription surface | `audio_transcription` |

All three are `hosted_vllm`-class pods. A path table cannot separate rows 1 and 2; that is why the current chat-first rule is *correct* for VibeVoice and *wrong* for Drama 3. No reordering of `_PATH_MODES` fixes that, and neither does any other static table, because the information is not in the path list.

## The distinction that does work: mounted vs. usable

Probing the live pods shows the servers distinguish two failures that the controller currently conflates:

| Status | Meaning | Example |
| --- | --- | --- |
| `200` | Surface works | VibeVoice `POST /v1/chat/completions` with a text prompt |
| `400` | Surface is **mounted**, request rejected (bad body, **wrong modality**) | Drama 3 `POST /v1/chat/completions` -> "the speech input must be text" |
| `404`/`405` | Surface **not mounted** (or unknown model id) | Drama 3 `POST /v1/embeddings`, vLLM `POST /v1/chat/completions` with a bad model id |

A `400` from a *well-formed* request means "this endpoint exists and inspects my payload" — i.e. mounted but not the deployment's capability. A `404` means "this deployment doesn't serve this surface at all". That difference is the capability signal, and it is exactly what `probe_verdict_to_fact` in `controller/capabilities.py:91` was already built to classify: `200` -> accepts, content-refusal codes -> rejects, everything else -> unknown. The existing synthetic image/audio probes reuse this and are already memoized per deployment. The capability machinery is there; it is pointed at modalities, not modes.

Caveat found while validating: a `GET`-only pre-filter is **not** portable. vLLM answers `GET /v1/chat/completions` with `404` while happily serving `POST`, so mount detection must use the same method the capability would use. Probing `GET` to decide would misclassify real vLLM chat pods.

## Design: a capability probe table as data, with a declared override

Three layers, in precedence order.

### 1. Declared override wins (operator intent)

Reuse the ConfigMap field mechanism the controller already has (`local_deployments.py:122,173` reads `MODEL_ID` and `EXTRA_ARGS`). Add `MODE`:

```
MODE=audio_speech
```

This is for the cases where a human knows better than any probe: a pod that is temporarily unhealthy, a canary, a deployment whose intent is genuinely ambiguous. It is a data change, not a code change.

### 2. Probe table as data, not `if` conditions

The parent asked for a "better formatted json capability easy to edit list". Agreed, with one correction: the table should describe **capability probes**, not path-to-mode mappings, because the mapping is what keeps being wrong. Proposed shape, in `controller/capabilities.py` (or a sibling `controller/probes.py`), loaded from a YAML/JSON file the way `exclusions.py` and `sources_config.py` already do:

```yaml
probes:
  - capability: chat
    method: POST
    path: /v1/chat/completions
    body: {model: "$MODEL_ID", messages: [{role: user, content: "hi"}], max_tokens: 1}
    success: 200
  - capability: audio_speech
    method: POST
    path: /v1/audio/speech
    body: {model: "$MODEL_ID", input: "hi", response_format: wav}
    success: 200
    discard_response: true     # audio is large; never buffer it
  - capability: audio_transcription
    method: POST
    path: /v1/audio/transcriptions
    multipart: file=$SYNTH_AUDIO
    success: 200
  - capability: diarization
    method: POST
    path: /v1/diarize
    ...
```

Then the rule becomes a single ordered reduction, not a table of special cases:

- a capability whose probe **succeeds** (`200`) is available;
- a capability whose probe is a **content refusal** (`400/415/422`) is mounted-but-unusable, so it must **not** win;
- a capability that is **unknown** (`404`, timeout, 5xx) is not claimed.

Mode selection then walks the available set in **declared priority** (from the same file, e.g. `[chat, audio_speech, audio_transcription, diarization, embeddings, rerank, ocr]`), taking the first capability that succeeded. Drama 3: chat 400, speech 200 -> `audio_speech`. VibeVoice: chat 200 -> `chat`. Qwen3-ASR: transcription 200 -> `audio_transcription`. No vendor names in the code path.

Because the probe table is data, adding a new serving stack is a config change; the code only knows about `success` / `refusal` / `unknown` and the ordering rule.

### 3. Server family as a cheap hint, not the decider

The parent's instinct that sglang and vLLM are identifiable is right, and the image name is already available on the Deployment object (`dep.spec.template.spec.containers[0].image`), currently unused. Verified distinct: `sglang:`, `sglang-omni:`, `adeo-vllm:` in the registry path.

Use it to bound the probe set, not to determine the mode: an `adeo-vllm`/`sglang` image is known to serve the OpenAI family, so probe the standard set; an unknown image (custom fork, custom endpoint shapes) probes only what the declared table lists and otherwise defers to the ConfigMap. This keeps the fingerprint as an *optimization and default*, never as the source of truth — which is what makes it safe, since image names are not a contract and a custom fork can do anything.

Also note: `sglang-omni` answers `/model_info`, while plain `sglang` answers `/get_server_info` and 404s `/model_info`; `adeo-vllm` 404s both. So even "which metadata endpoint exists" already varies three ways. Another reason the capability has to be probed rather than inferred.

## Where custom providers fit

Custom providers (`hamsa`, `pyannote`, `omnivoice`) remap a LiteLLM path to a pod-specific path. Under this design they stay exactly as they are: their pods fail the standard probes (so no standard capability is claimed), the ConfigMap `MODE` declares the intent, and `detect_provider` still picks the custom config by name. The probe table supplies the *mode*, the provider registry supplies the *transport*. Those are orthogonal, which is why no capability logic needs to know about them.

## Cost

Measured live on the Drama 3 pod: a chat probe returning 400 takes ~326 ms; a minimal speech probe (~1 s of audio) takes ~537 ms; eight `GET` mount checks take ~2.6 s in total but are not method-safe (see caveat). Memoize per `(uuid, image-digest)` the way `_synthetic_probe_cache` already memoizes modalities, and the steady-state cost is one probe set per deployment redeploy. Stop probing a capability once it succeeds.

## Test strategy

The `probe_verdict_to_fact` helper and its `_CONTENT_REFUSAL_CODES` set are the pivot, so pin them first. Then table-test the reduction: `[chat=200, speech=400]` -> `chat`; `[chat=400, speech=200]` -> `audio_speech`; `[chat=404, speech=200]` -> `audio_speech`; `[chat=unknown]` -> no claim, keep previous mode. The last case matters: an unreachable probe must never demote a working deployment.

## Summary of the change from the previous design

| Previous | This design |
| --- | --- |
| Reorder `_PATH_MODES` | Static path tables are the bug, not the fix; probe mutation |
| ConfigMap `MODE` only | ConfigMap stays, but as override, not the primary mechanism |
| Serve-intent via `supported_task_types` | Pods don't publish it; probe behavior instead |
| Per-provider `fishaudio` slice | Capability is orthogonal to provider; unchanged |

The `hosted_vllm` TTS pass-through config from the previous document is still worth doing, and is now independent: that one fixes **request field forwarding**, this one fixes **registration**. They should not be bundled.
