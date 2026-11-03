# Speaker Embeddings in `/v1/diarize` — Work Plan

> **Request source**: consumer-team feedback (2026-11-03): return per-speaker
> voice vectors from the pyannote deployment for their voice-ID flow
> (they match a vector to a confirmed name, i.e. enrolment by confirmation).
> **Status**: DELIVERED 2026-11-03 — live on the cluster. speakrs 0.7.1
> (commit `cf9e45b`, wheel `speakrs-0.7.1-cp311-abi3-manylinux_2_35_x86_64.whl`),
> coro `00bcdcf` on `jya0-v1.5.2`, image `ffdabc3508f1`, pod
> `j-d4398150-0202-4e1c-ae9a-41be01288fd3-86cfd784fd-gcc9m`. Live check
> passed: keys == speakers, 256 floats, revision `67f963b6…`, and the
> default response stays byte-identical without the flag.
> **Related**: [PYANNOTE_DIARIZATION_PROVIDER_PLAN.md](../custom-providers/PYANNOTE_DIARIZATION_PROVIDER_PLAN.md)
> (original provider slice), [usage-guides/PYANNOTE_DIARIZATION_GUIDE.md](../usage-guides/PYANNOTE_DIARIZATION_GUIDE.md)
> (current contract + benchmarks).

---

## 1. What was asked

Add per-speaker voice vectors to the `/v1/diarize` response for the
consumer's voice-ID flow (they match a vector to a confirmed name, i.e.
enrolment by confirmation). **v2 scope decision**: the extension is
**opt-in via a request flag** so the official `pyannoteai-sdk` client keeps
working byte-identically forever; consumers who send the flag get:

```json
"output": {
  "diarization": [...],
  "exclusiveDiarization": [...],
  "speakerEmbeddings": {
    "SPEAKER_00": [256 floats],
    "SPEAKER_01": [256 floats],
    "SPEAKER_02": null
  },
  "embeddingModel": {
    "id": "pyannote/speaker-diarization-community-1",
    "revision": "<pinned HF commit sha>",
    "dim": 256
  }
}
```

Rules from the request:

1. **Additive only** — existing fields unchanged, current consumers unaffected.
2. **Keys match labels** — `speakerEmbeddings` keys are exactly the
   `SPEAKER_xx` strings appearing in `exclusiveDiarization`.
3. **Padded rows are `null`** — a zero-padded speaker row must serialize as
   `null`, not a vector of zeros.
4. **Revision pinned and returned** — vectors from different model revisions
   are not comparable; consumers must be told before an upgrade so members
   re-record voice IDs.

Cost: none extra at inference time (vectors already computed internally);
response grows ~3 KB per speaker.

## 2. Where the vectors live today (verified in source)

The engine computes the right artifact already and throws it away at the SDK
boundary. Chain:

| Layer | File (branch `master` = speakrs 0.7) | What exists | What's missing |
| --- | --- | --- | --- |
| Engine | `src/pipeline/clustering.rs:136` `weighted_centroids()` | One weighted 256-d centroid per kept speaker (VBx posterior-weighted mean of chunk embeddings — a soft assignment, better than a hard mean) | Returned nowhere; `TrainingEmbeddings::cluster()` returns only `ChunkSpeakerClusters` |
| Engine | `src/inference/embedding.rs:42` | `EMBEDDING_WIDTH = 256` | — |
| Engine | `src/pipeline/types/data.rs:274` | `DiarizationResult` carries `embeddings: ChunkEmbeddings` (per-chunk) + `hard_clusters` | No per-speaker centroid field |
| Engine | `src/segment.rs:53` | Speaker labels minted as `SPEAKER_{idx:02}` from the activation column index | — (label ↔ centroid row identity, see §4) |
| SDK | `crates/speakrs-sdk/src/pipeline.rs:319` `to_diarization()` | — | Discards everything but segments; must carry centroids + labels |
| Bindings | `crates/speakrs-python/src/results.rs` `PyDiarizationResult` | `segments`, `exclusive_segments`, `speakers`, `model_revision` getters | No `speaker_embeddings` getter |
| coro | `coro/backends/diarization/speakrs.py` `diarize_pcm_full()` | returns `(segments, exclusive_segments)` | Must return embeddings + revision too |
| coro | `coro/api/pyannote/diarize.py` | pyannoteAI-exact `DiarizationJobOutput` | Add the two new output fields |
| LiteLLM | `litellm/llms/pyannote/diarization/transformation.py:86` | `transform_diarization_response` = `raw_response.json()` — **pure passthrough** | **Nothing.** New fields flow through unchanged. |

## 3. Is this what the pyannoteAI cloud endpoint provides?

**No — and that is fine, but it must be a conscious decision.**

Checked against the pinned official pyannoteAI OpenAPI spec
(`coro/tests/api/pyannote/pyannoteai-openapi-v1.json`, fetched from
docs.pyannote.ai 2026-10-19). The official `DiarizationJobOutput` schema
defines exactly:

| Field | In official spec | In our response after this change |
| --- | --- | --- |
| `diarization` (required) | yes | yes (unchanged) |
| `exclusiveDiarization` | yes | yes (unchanged) |
| `confidence` | optional | 400-rejected (engine can't produce) |
| `speechProbability` / `crosstalkProbability` / `speakerProbability` | optional | 400-rejected |
| `wordLevelTranscription` / `turnLevelTranscription` | optional | 400-rejected (use `/v1/audio/transcriptions`) |
| `error` / `warning` | optional | `warning` yes; `error` unused |
| **`speakerEmbeddings`** | **absent** | **new, ours** |
| **`embeddingModel`** | **absent** | **new, ours** |

So the response becomes a **superset** of the cloud contract: every
official field keeps its exact shape, and two self-hosted extensions are
added inside `output`. This is the same posture the route already has for
transport (`file` upload instead of the cloud's presigned-URL `url`) —
exact where the spec speaks, explicit where self-hosting legitimately
diverges. Consequences we accept:

- The official `pyannoteai-sdk` client ignores unknown `output` keys (dicts),
  so it keeps working. LiteLLM's provider is passthrough, so it keeps
  working. No existing consumer can break (additive JSON keys).
- The spec-conformance suite must be updated **deliberately**: it currently
  asserts our `DiarizationJobOutput` matches the pinned schema
  field-for-field. It must move to a superset assertion: official fields
  stay pinned (shape + required-ness), the two extensions get their own
  pinned assertions (presence, key-matching rule, dim, null rule), so a
  future upstream spec refresh still fails loudly on drift.
- If pyannoteAI ever ships embeddings upstream, we reconcile then
  (rename/align — likely trivial since our shape matches their request
  template).

## 4. Design decisions (v2 — flag-gated, official-SDK-compatible)

1. **Opt-in via a body flag: `embeddings` (optional, default absent).**
   Replaces the earlier always-on idea after review. Rationale:
   - **Official SDK compatibility is the hard requirement**: the official
     `pyannoteai-sdk` client posts exactly the spec's `DiarizeRequest`
     fields. An *optional extra field that is absent by default* keeps
     byte-identical requests from the official client passing through the
     gateway untouched — nothing to opt out of, nothing to strip.
   - The extension behaves exactly like our transport divergence (`file`
     multipart instead of the spec's `url`): the request surface stays
     spec-shaped, self-hosted extensions are additive options.
   - Absent → spec-exact response (no new keys at all). Present → the two
     additive output fields. Consumers who don't ask see **zero** change.
   - `embeddings=false` behaves exactly like absent (spec-exact), so the
     flag never has three states to reason about.
2. **Where the flag lives end-to-end (all layers follow the same rule:
   absent means absent):**
   - coro route: `embeddings: bool = Form(default=False)` — the *only*
     new request-side line; **not** added to `_UNSUPPORTED_FIELDS` (it is
     our own extension, not a spec field we reject).
   - coro pipeline: `diarize_full(audio, *, exclusive, embeddings)` —
     one keyword with a default; non-speakrs backends ignore it (they
     already raise for `diarize_full` itself, so no new error paths).
   - speakrs wheel 0.7.1: **zero request-side change.** The result object
     grows a `speaker_embeddings` getter (always computed by the engine
     anyway, ~µs) and coro *reads it only when the flag is set* and
     otherwise serializes exactly as today. This keeps the wheel API
     additive-only — no config, no breaking method signatures.
   - LiteLLM: **zero change.** The provider is already a multipart
     passthrough (`transform_diarization_request` forwards `form_fields`
     verbatim; `transform_diarization_response` returns
     `raw_response.json()`). An `embeddings=true` form field flows to the
     pod; new `output` keys flow back.
3. **Which vector: the weighted centroid** (unchanged from v1).
   `weighted_centroids()` — VBx posterior-weighted mean of chunk
   embeddings, the representation the engine itself uses for assignment.
   Not L2-normalized; document and keep byte-stable across versions.
   Note: this matches the official `pyannote.audio` 4.x
   `DiarizeOutput.speaker_embeddings` semantics (hard-mean centroids
   there, posterior-weighted here — both 256-d wespeaker-family
   embeddings, labels-order-aligned rows, zero-pad → null).
4. **Label ↔ row identity guarantee** (unchanged). `SPEAKER_%02k` ↔
   centroid row *k* by construction (`src/segment.rs:53` mints labels
   from the same activation column index the reconstruction step uses);
   pinned by a unit test.
5. **Null vs pruned** (unchanged). speakrs prunes sub-threshold speakers,
   so in practice every label gets a vector; the all-zero-row → `null`
   rule is implemented anyway to match the requested contract.
6. **Revision source** (unchanged). `model_revision` = pinned commit
   `67f963b6…`; `embeddingModel.id` = `pyannote/speaker-diarization-community-1`
   (internal engine id; the request `model` field stays the serving id
   `pyannote/community-1`); `dim` = 256 from `EMBEDDING_WIDTH` — from a
   constant, never probed from data (the spike's dim-probe bug fixed).
7. **Corrections to the requester's runtime table** (their guide assumes
   pyannote.audio 4.x on torch): `torch.set_flush_denormal` — N/A, our
   engine is Rust + CUDA. `PYANNOTE_METRICS_ENABLED` /
   `HF_HUB_DISABLE_TELEMETRY` — N/A, no telemetry exists in the Rust
   stack. "Pin the revision" — already true since 0.7 (`MODEL_REVISION`
   constant). "Short calls under 90 s" — trivially met: 30–45 s enrolment
   audio diarizes in <1 s engine time (727× single-stream RTFx measured).
8. **What we do NOT take from the consumer's spike** (`pyannote-service.zip`,
   reviewed): his `revision="local"` default (we return the real pinned
   sha), his `num_speakers` pass-through (our contract 400s it — his
   request example doesn't send it anyway), and his data-probed `dim`
   (constant here). His core insight — return the embeddings the model
   already computes — is correct and is what this plan implements on the
   production engine.

## 5. Task breakdown

### Phase 1 — speakrs engine (`master`, release 0.7.1)
- [x] 1.1 `TrainingEmbeddings::cluster()` (`src/pipeline/clustering.rs`):
      return `(ChunkSpeakerClusters, Option<Array2<f32>>)` — centroids
      already computed at line 136; the `< 2 rows` fast path (single
      cluster) synthesizes a mean-of-active-embeddings centroid.
- [x] 1.2 Thread centroids through the pipeline into
      `DiarizationResult` (`src/pipeline/types/data.rs`): add
      `centroids: Option<Array2<f32>>`.
- [x] 1.3 SDK (`crates/speakrs-sdk/src/pipeline.rs`): `Diarization` gains
      `speaker_centroids: Option<Vec<Vec<f32>>>`; `to_diarization()` maps
      row-major.
- [x] 1.4 Bindings (`crates/speakrs-python/src/results.rs`): getter
      `speaker_embeddings -> dict[str, list[float] | None]` built from
      centroids + the same `SPEAKER_%02k` minting; all-zero row → `null`.
      Also expose `EMBEDDING_WIDTH` as `speaker_embedding_dim` or a
      constant in `speakrs.__init__`.
- [x] 1.5 Tests: fixtures for the clustering stage already exist
      (`fixtures/pipeline_*` `.npy`); add a test that runs the fixture
      pipeline and asserts (a) centroid row count == distinct labels,
      (b) label ↔ row identity, (c) dim == 256, (d) revision populated.
- [x] 1.6 Bump version 0.7.0 → 0.7.1, CHANGELOG entry.

### Phase 2 — wheel
- [x] 2.1 `maturin build` Linux x86_64 + CUDA (the existing release path;
      `just` target if present).
- [x] 2.2 Validate in a local container (CPU mode OK): diarize an AMI
      clip; assert keys == `exclusiveDiarization` speakers, 256 floats,
      revision string, `null` handling.

### Phase 3 — coro (`jya0-v1.5.2`)
- [x] 3.1 Stage the 0.7.1 wheel into `docker/wheels/` (Makefile wildcard
      already picks up any `speakrs-*.whl`).
- [x] 3.2 `DiarizeResult` (`coro/pipelines/full_memory.py`) gains
      `speaker_embeddings: dict | None` and `model_revision: str | None`;
      `diarize_full()` gains `embeddings: bool = False` keyword and
      populates them only when set (None otherwise — one field, one
      default, non-speakrs backends unchanged: they already raise for
      `diarize_full` itself).
- [x] 3.3 Adapter (`coro/backends/diarization/speakrs.py`
      `diarize_pcm_full`): return the embeddings dict + revision from the
      native result when the pipeline asks for them; keep the 2-tuple
      call path untouched for `embeddings=False` (the flag is a keyword
      with a default on `diarize_full`, the adapter always reads the
      now-cheap `result.speaker_embeddings` getter and the pipeline
      discards it when the flag is off — no branching in the adapter).
- [x] 3.4 Route (`coro/api/pyannote/diarize.py`): ONE new Form param
      `embeddings: bool = Form(default=False)` (NOT added to
      `_UNSUPPORTED_FIELDS` — it is our own extension, not a spec field
      we reject); `DiarizationJobOutput` gains optional
      `speakerEmbeddings: dict[str, list[float] | None]` and
      `embeddingModel: {id, revision, dim}` — serialized by the existing
      `exclude_none`, so **absent flag → absent keys** (spec-exact
      response, official SDK byte-compat).
- [x] 3.5 Update conformance suite (`tests/api/pyannote/`):
      - default-path assertion stays a strict field-for-field match
        against the pinned official schema (the flag changes nothing
        there — this is the official-SDK guarantee, pinned by test);
      - new opt-in assertions: with `embeddings=true`, keys ==
        exclusive diarization labels; every value 256 floats or `null`;
        `embeddingModel.revision` == the pinned commit; `id` == internal
        engine id; and with the flag absent, `speakerEmbeddings` /
        `embeddingModel` are **not present at all**.
- [x] 3.6 API tests: opt-in response-shape test; default-path regression
      test proving the response without the flag is byte-identical to
      today's (existing tests + the conformance pin cover it); run full
      suite.

### Phase 4 — deploy & verify
- [x] 4.1 `make -C docker adeo` (wheel layer change → wheel + sync layers
      rebuild; weights/canary layers dedupe), `adeo-push`.
- [x] 4.2 Roll the pod; health; then the requester's own "done when" jq
      check through the gateway WITH `-F embeddings=true` added:
      `jq '{status, speakers: [.output.exclusiveDiarization[].speaker] | unique, vectors: (.output.speakerEmbeddings ...), model: .output.embeddingModel}'`
      — one key per speaker, 256 numbers or null, revision a commit sha.
      DONE in-pod on a 90 s AMI clip: 3 speakers, norms 2.639/1.811/0.983,
      `embeddingModel = {"id": "pyannote/speaker-diarization-community-1",
      "revision": "67f963b6a1d94fcd4678948237b3cc8bd69bf1d8", "dim": 256}`;
      default (no flag) verified absent-keys.
- [ ] 4.3 Re-run the in-pod benchmark to confirm no throughput regression
      (embeddings add ~3 KB/speaker serialization only). — NOT YET RUN
      (deferred; the embeddings add serialization only, no extra GPU work:
      they come from the same run's result getters).

### Phase 5 — docs
- [x] 5.1 Usage guide: new response fields, the additive-superset
      relationship to the pyannoteAI spec, voice-ID usage note (vectors
      are NOT L2-normalized; stable per revision; re-enrol after upgrade),
      and the runtime-table corrections (§4.6).
- [x] 5.2 This plan doc: tick the boxes, record the delivered revision.
- [x] 5.3 Memory files: record the centroid plumbing decision.

## 6. Official pyannote SDK compatibility (the v2 driver)

The official `pyannoteai-sdk` client must keep working unchanged, forever.
Verified mechanics:

- The SDK posts exactly the spec's `DiarizeRequest` fields — it will never
  send `embeddings`. Absent flag → coro's `Form(default=False)` → the
  response contains only spec-shaped keys. **Byte-identical to today.**
- The SDK's `retrieve()`/response handling tolerates extra keys on its own
  (it returns plain dicts), and we never add keys unless asked anyway.
- LiteLLM passes unknown request fields through as multipart form fields
  (`transform_diarization_request` copies `form_fields` verbatim) and
  returns the pod's JSON verbatim (`transform_diarization_response`) —
  the flag and the new response fields need **zero gateway changes**.
- Curl/plain-HTTP consumers add one form field:
  `-F embeddings=true`.
- The spike's `/v1/embed` (single-range voice-print) is out of scope for
  the production route; his notebooks can keep using his local spike for
  that, or a follow-up adds ranges to the same flag.

## 7. Risks / open questions

- **Cluster fast-path** (`< 2 training rows`): synthesized centroid must
  still match label indexing; covered by 1.5 tests.
- **Byte-stability of vectors across patch versions**: we pin the engine
  wheel version in the image; a future speakrs upgrade that changes
  clustering defaults silently invalidates consumer enrolment vectors —
  must be called out in the guide's upgrade note (§5.1) and in the
  deployment manifest.
- **Response size**: 8-speaker meeting ≈ 8×256×~9 chars ≈ 20 KB — well
  within JSON response norms; no pagination needed.
- **Open**: should `embeddingModel.id` be the internal engine id
  (`pyannote/speaker-diarization-community-1`) as the requester wrote, or
  the serving id (`pyannote/community-1`)? Plan assumes the requester's
  literal (internal id) — confirm with them only if it matters for their
  vector-comparison logic (it shouldn't; the revision is the load-bearing
  field).
