# Capability registration: review of the probe design, and the simpler answer the data already gives

A second review of [CAPABILITY_REGISTRATION_BY_PROBE.md](CAPABILITY_REGISTRATION_BY_PROBE.md), asked for by the parent. Verdict up front: the probe design is sound as a *last-resort tier*, but verification of what already flows through the controller found a declaration layer that makes most of the probing unnecessary. The probe table should be demoted from primary mechanism to tie-breaker. Three findings, in order of importance.

---

## Finding 1: OICM already declares the capability, and the controller drops it

The parent asked to reuse existing logic. There is more to reuse than expected: the `deployment_summary` payload the status poller already fetches every 10 seconds carries an **`inference_task`** field — OICM's own `ModelServerTasks` enum, derived from the HuggingFace task taxonomy — plus `model_server_name` and `model_server_tag`. The operator picked the task when deploying the model. It is the declaration of intent, arriving continuously, and `OicmDeploymentSummary` (`controller/status/wire.py:85`) throws it away because the field is simply not declared on the DTO.

Verified live on both sources (Al Ain 27 deployments, Abu Dhabi 1):

| Deployment | `inference_task` | `model_server_name` |
| --- | --- | --- |
| `fishaudio/drama-3` (51096511) | `Text-to-Speech` | None (`custom_model_server` image instead) |
| `microsoft/VibeVoice-ASR` (061fbb9d) | `Automatic Speech Recognition` | `vLLM` |
| `Qwen3-Next`, `DeepSeek-V4-Flash` | `Text Generation` | `vLLM` / `SGLang` |
| `Qwen3-Embedding-*` | `Text Embedding` | `vLLM` |
| `Qwen/Qwen-Image-2.1` | `Text-to-Image` | `SGLang` |
| coro (d4398150, diarization) | None (custom image path) | None |

The enum has 49 values including `Diarization`, `Text Ranking`, `Question Answering`. Both OICM versions (Al Ain 1.15.19 and AD 1.7.1) return it.

This is strictly better than my ConfigMap `MODE` proposal: same authority (the operator's deploy-time declaration), but no new mechanism to wire, no ConfigMap to edit per deployment, and it flows through plumbing that already exists and is already tested.

**Design consequence:** mode selection gains a first tier with zero new infrastructure:

```
tier 0: inference_task from deployment_summary     (declared, per-deployment)
tier 1: ConfigMap MODE override                    (escape hatch for tier-0 mistakes)
tier 2: capability probes                           (last resort when tier 0 is absent)
tier 3: path-shape detection                        (today's fallback, stays as the floor)
```

The mapping from `ModelServerTasks` to LiteLLM mode is a small static table in the controller, data not code — exactly the "better formatted json capability easy to edit list" the parent asked for, but populated from OICM instead of hand-maintained:

```json
{
  "Text Generation": "chat",
  "Image-Text-to-Text": "chat",
  "Text-to-Speech": "audio_speech",
  "Automatic Speech Recognition": "audio_transcription",
  "Text Embedding": "embedding",
  "Text-to-Image": "image_generation",
  "Diarization": "diarization"
}
```

Unmapped tasks degrade to tier 2, never crash.

### The hole this closes that probing cannot

Probes observe the surface; they cannot observe *intent*. VibeVoice-ASR is the standing example: `POST /v1/chat/completions` returns 200 there, so probing classifies it `chat`, while OICM says its task is `Automatic Speech Recognition`. Both are true — the *capability* is chat, the *task* is ASR. Under a probe-primary design the row reads `chat` and the ASR declaration is lost. Under tier-0-primary the row carries both facts: `mode=chat` (from the task mapping, see finding 3) and the raw task preserved for consumers. The declaration and the observation are different facts and the record should keep both.

---

## Finding 2: the probe design's holes, and what survives

Honest holes in CAPABILITY_REGISTRATION_BY_PROBE as written:

**Cost under churn.** Every `_handle_add` runs discovery, which would run the probe set. A pod that crash-loops repays the full probe cost on every restart, and probes are real inference (a TTS probe generates audio). Mitigation exists but must be explicit: memoize on `(uuid, pod-spec image digest)`, and skip probes entirely when tier 0/1 already decided.

**Probes mutate the deployment's state.** sglang counts requests; a probe inflates its metrics, and a 400 from a TTS pod still writes a log line per probe. Individually trivial, multiplied by 300 s sync cycles and 27 deployments it is noise the platform team then has to filter. Mitigation: probe once per (uuid, digest) and stop once a capability is confirmed.

**A 400 is not always a modality refusal.** `probe_verdict_to_fact` treats 400 as rejection, but a pod can 400 for a malformed probe body (our bug) as easily as for a wrong modality (their state). If the probe table's body drifts from what the server accepts — say a `max_tokens` field the new server version rejects — every pod silently classifies as "not chat". Mitigation: a probe result must carry its evidence (the response snippet) into the log, and an unexpected-4xx rate above some threshold should page rather than silently demote 27 models.

**Diarization's probing is awkward.** coro accepts a multipart upload; a minimal probe needs a real audio file shipped in the probe table. That is fine, but it means the probe config format needs multipart support and an inline audio fixture, which is the most complex entry in the file by far.

**The double-404 trap.** vLLM answers 404 both for "route not mounted" and for "unknown model id" — verified live when my probe used a wrong model id. Any probe runner must read `/v1/models` first and substitute the real id, or the whole table misclassifies. This is a correctness requirement on the runner, not a design flaw, but it is the kind of detail that silently breaks.

What survives review: the three-verdict classification (`200` works / content-refusal mounted-but-wrong-modality / unknown no-claim) is exactly right and `probe_verdict_to_fact` already implements it. The probe table as data survives. Method-matching (probe with the capability's own HTTP method) survives — the GET finding was correct and important.

---

## Finding 3: reuse the `/endpoints` status layer for the residual ambiguity

The parent asked what can be reused from the `/api/v1/models/x/x/endpoints` logic. Concretely reusable, verified:

**`GatewayStateResolver` (`gateway_status.py:132`) is the pattern to copy, not the code to call.** It maps one set of observed facts to one verdict with a documented decision record, keeps facts separate from presentation (`serving_available` separate from `availability`), and owns staleness as a first-class concern. The task-to-mode mapping should be built the same way: a frozen dataclass in, a `GatewayStatus`-style verdict out, staleness and unknown mapped to "claim nothing, keep the previous mode" — never a silent demotion. The controller's change-guard philosophy (`_patch_is_noop` only writes when a compared fact moved) is the write-amplification guard, already in `reconciler.py`.

**`supports_diarization` in `mapping/endpoints.py:142` shows the task belongs in `model_info`.** It reads `deployment.model_info.get("mode") == "diarization"` today. Once tier 0 lands, the raw `inference_task` string should also be stamped into `model_info.oicm_task` so the public endpoint surface can key off the declaration instead of inferring from mode. That is a two-line change in `_register_payload`/`patch_status` and it makes the whole chain — OICM declaration to public API — inspectable end to end.

**The `oicm` status block is the proven transport.** `model_info.oicm` already flows (v, status, serving_available, replicas, observed_at) and `StatusPersister` already writes it with a change guard and heartbeat staleness. Adding `inference_task` to the wire DTO (`wire.py:85`), the snapshot (`snapshot.py:28`), and the persisted block is the same shape of change that added `oicm_cluster` — the memory notes record exactly this precedent.

### The VibeVoice resolution, concretely

With all four tiers, VibeVoice resolves correctly at every level:

- tier 0: `Automatic Speech Recognition` maps toward `audio_transcription`
- but the serving surface is chat, so the **transport probe or path detection** must be allowed to override toward `chat` when the native surface is not mounted

So the final rule is not "tier 0 wins, full stop". It is: **tier 0 decides the task; the transport layer decides how to serve it; when they disagree, the disagreement is recorded, not silently resolved.** Concretely: `mode` follows the mounted surface (VibeVoice ends `chat`, as today), and `model_info.oicm_task` preserves the declared ASR intent. The endpoints layer can then advertise "ASR served via chat" honestly instead of losing one of the two facts. This is the same facts-vs-presentation split `GatewayStateResolver` already models.

---

## Revised recommendation

1. **Stop dropping `inference_task`.** Add it to `OicmDeploymentSummary` + `OicmStatusSnapshot` + the persisted `oicm` block, and stamp `model_info.oicm_task`. Pure additive plumbing through tested code, fixes the data availability for everything downstream. Smallest possible first step.
2. **Task-to-mode mapping as data in the controller.** A static table, degraded by tiers 1-3 where the task is absent or ambiguous. Fix Drama 3 by declaring, not probing.
3. **Keep the probe table as the last-resort tier only.** For `custom_model_server` deployments whose task is None and whose ConfigMap carries no MODE (coro today registers correctly through path detection, so this tier exists for the *next* unknown pod).
4. **Path detection stays as the floor.** It already handles Hamsa's native surface and OCR. Reordering `_PATH_MODES` (TTS before transcription) remains a small, correct fix.

This keeps the probe design's real contribution (the three-verdict semantics, method-matched probes, table-as-data) while making the declared task — the strongest, cheapest, already-flowing signal — the primary tier. The gateway keeps its single job: serve what the controller registered, with the mode and the task both in the row.
