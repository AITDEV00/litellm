# Vertical Slice Technique — Deterministic Placement of OICM Custom Code

This is the procedure for adding or changing behavior that upstream LiteLLM does not have. It exists because this repository is a fork: every custom line placed inside an upstream-managed file is a future merge conflict and a future silent regression, and both have happened.

Follow it in order. Each step has a pass/fail condition. If a step fails, stop and do not proceed to the next one. The companion document [`additional-features-origin.md`](../additional-features-origin.md) is the map of every slice that exists today; read it before writing new code so you extend a slice instead of duplicating one.

## The one invariant

**A custom feature lives in a file the fork owns, and upstream files change only by adding a single import or mount line.**

That is the whole architecture. Everything below is the mechanical procedure for holding that line. If you remember one rule, remember this one; the rest is how to check it.

## Step 1 — Classify the change

Answer these in order. The first one that matches decides the placement, and you stop.

| # | Question | If yes | If no |
|---|---|---|---|
| 1 | Does behavior for this feature already exist in a slice listed in `additional-features-origin.md`? | **Extend that slice.** Do not create a second home. Go to Step 2. | Next question |
| 2 | Does the file you would naturally edit belong to upstream (present in the upstream tag, not added by this fork)? | **Create a new slice** (Step 3) with one mount line in the upstream file. | Next question |
| 3 | Is this a purely additive new capability (a route, an endpoint, a provider, a tracer, a hook) with no existing OICM home? | **Create a new slice** (Step 3). | Next question |
| 4 | Is this a behavior change inside an OICM-owned file only? | **Edit in place.** No slice needed. Go to Step 5. | Stop and ask. You have a change that spans upstream and OICM logic with no clean seam, which is the case this technique cannot decide mechanically. |

**How to check "belongs to upstream" without guessing:**

```bash
# Author history tells you who owns the file
git log -1 --format='%h %an' -- path/to/file.py
```

Every upstream file shows an upstream author on its last relevant commit. Every OICM slice shows `jya0`. Do not infer ownership from directory location, because slices are co-located inside upstream directories on purpose.

## Step 2 — Extending an existing slice

1. Edit the slice's own files. This is always safe; upstream will never touch them.
2. If you need a new symbol reachable by callers, add it to the slice and re-export it where the existing exports live (see Step 4).
3. If you add a user-visible behavior, add a case to the drop-detection test. If you add a route or response shape, add a contract test.
4. Stop here. Nothing else to decide.

## Step 3 — Creating a new slice

**3a. Choose the location by mirroring upstream's own layout.** Put the slice next to the thing it extends, using upstream's naming conventions. Concretely:

| Upstream shape | OICM slice goes | Existing example |
|---|---|---|
| `litellm/endpoints/<feature>/` | a sibling package `litellm/endpoints/<new_feature>/` | `litellm/endpoints/voice/` |
| `litellm/proxy/<x>_routes.py` | `litellm/proxy/<feature>_routes.py` | `voice_routes.py`, `diarization_routes.py`, `oicm_routes.py` |
| `litellm/llms/<area>/` | `litellm/llms/oicm_providers/` | provider config registry |
| `litellm/integrations/<area>/` | `litellm/integrations/prometheus_helpers/` | in-flight gauge |
| `litellm/litellm_core_utils/<x>.py` | `litellm/litellm_core_utils/<feature>.py` | `stream_tracer.py` |

Never create a parallel top-level tree (no `oicm/` at the repo root). A new top-level directory is visible in every merge and defeats the purpose.

**3b. Use the minimum file set.** A route slice is one module exposing a `router`. A library-level slice is one package with an `__init__.py` that exports its public names. Do not create `routes.py` plus `service.py` plus `schema.py` layers by default; split only when one file exceeds roughly 400 lines or holds two genuinely unrelated concerns. The generative template in [`logic_mapping_technique.md`](logic_mapping_technique.md) Phase 3 describes a layered variant; it is a permissible shape for a large new subsystem, not the default for a small feature.

**3c. Enforce the boundary inside the code.** Import from upstream; never reach sideways into another slice's internals. Slices may import `litellm.*` and shared helpers, and nothing else from an OICM slice except its public exports.

## Step 4 — Wire it with exactly one touch per upstream file

Each upstream file you must modify gets **one** change: an import, plus at most one mount or registration call. Nothing else.

| Wiring type | Upstream file | Shape of the change | Example |
|---|---|---|---|
| Route | `litellm/proxy/proxy_server.py` | one import, one router include | `from litellm.proxy.voice_routes import router as oicm_voice_router` |
| Library export | `litellm/__init__.py` | one entry in the lazy `__getattr__` map | `acreate_voice` resolves to `litellm.endpoints.voice.main` |
| Callback / hook | callback registration site | one registration line | HTB rate limiter |
| Provider dispatch | `litellm/utils.py` or the registry | one dispatch delegation | `oicm_providers` |

Rules that make this deterministic:

- **One import line per upstream file.** If your change needs three upstream edits in one file, the slice boundary is wrong; widen the slice instead.
- **Never edit an upstream function body.** If you find yourself inside one, extract the logic into the slice and make the upstream body a single delegation call.
- **No monkeypatching.** Patching an upstream symbol at import time is an invisible dependency that a merge can silently reorder.
- **Preserve the public API.** Callers must keep resolving the same name through the upstream facade; they should not need to know the slice exists.

## Step 5 — Pin it with a test that fails if the wiring disappears

Every slice gets at least one assertion in `tests/unit/proxy/test_oicm_drop_detection.py`. The test must assert the wiring, not the implementation:

| Slice type | Assertion to write |
|---|---|
| Router | the app contains the router's routes, and the module exposes `router` |
| Lazy export | `getattr(litellm, "<name>")` resolves to the slice's function |
| Callback | the callback is present in the registered callback list |
| Provider dispatch | the dispatch path reaches the slice |

The acceptance bar is mutation, not coverage: **delete the mount line and the test must fail.** If deleting the wiring leaves the suite green, the test is decorative and does not count.

If the feature is user-visible through the gateway, also add a case to `tests/smoke/` proving it works through the real route. Wiring tests prove it is mounted; smoke tests prove it works.

## Done criteria

A change is complete only when all of these are true. Check them explicitly rather than assuming:

1. Classification recorded: which Step 1 row applied, and therefore why this placement.
2. Custom logic lives in a fork-owned file; upstream files changed only by import or mount lines.
3. No new top-level directory.
4. Every upstream edit is a single line or a single-line delegation.
5. A drop-detection assertion exists that fails when the wiring is removed.
6. `additional-features-origin.md` has a row for the new slice.

## Failure modes this prevents

These are the real defects this technique exists to stop, each of which has occurred here:

- Custom code grafted inline into `proxy_server.py`, then clobbered by the next upstream merge.
- A gate dropped during a merge so every request takes the wrong path (the OCR `_RUST_OCR_PROVIDERS` loss, restored in `031043b779`).
- A URL builder regression from a merge, fixed in `147a4e93b6`, that made the TTS surface return 405.
- A provider dispatch delegation silently deleted, leaving an import that resolves to upstream's default.

## When this technique does not apply

- The change is a one-line fix inside an OICM-owned file. Edit it; do not manufacture a slice.
- The behavior genuinely belongs to upstream and should be contributed there instead. A fork slice carries permanent merge cost, so prefer upstreaming.
- The change spans two upstream files with no single seam. Stop and redesign; do not pick a placement silently.

## Related

- [`additional-features-origin.md`](../additional-features-origin.md) — the live map of every slice and where it lives
- [`logic_mapping_technique.md`](logic_mapping_technique.md) — the trace-before-build workflow that produces the logic map Phase 3 consumes
- [`upstream_merge_technique.md`](upstream_merge_technique.md) — the merge procedure that depends on this structure
- [`code_smell_detection_technique.md`](code_smell_detection_technique.md) — the audit that finds slices reduced to dead code
