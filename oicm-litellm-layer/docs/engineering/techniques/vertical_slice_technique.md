# Vertical Slice Architecture — A Complete Technique

A vertical slice architecture groups code by **the behavior it implements**, not by the technical layer it belongs to. One folder holds everything needed to fulfill one use case: its entry point, its logic, its data shapes, and its tests. Changing that behavior means opening that folder.

This document is the full technique: the principle, the rules, how to apply them, when not to, and where this repository already breaks them.

## 1. The principle

From Jimmy Bogard's [original description](https://www.jimmybogard.com/vertical-slice-architecture/) of the pattern, the governing sentence is:

> Minimize coupling between slices, and maximize coupling in a slice.

Read the second half first, because it is the one people skip. **Maximize coupling in a slice** means the files inside one folder may reference each other freely and directly, with no interfaces between them. A resolver may call a normalizer, read the slice's models, and use a private helper. That is not a problem to abstract away, it is the point. The cost of churn is confined to one folder.

**Minimize coupling between slices** means two slices do not reach into each other. If slice A needs work from slice B, B exposes one public entry point and A uses only that.

The contrast with a layered architecture, where every feature is smeared across horizontal tiers:

| | Layered | Vertical slice |
|---|---|---|
| Organizing axis | technical role (`models/`, `services/`, `utils/`) | behavior (`pricing/`, `fallbacks/`) |
| To add a feature | touch 4-6 directories | add or edit one folder |
| Change is contained by | convention and discipline | the filesystem |
| Coupling direction | every feature couples to every layer | slices couple to nothing |
| Test location | separate `tests/` tree, path mismatch common | beside the code, or mirrored deterministically |

The layered alternative is not wrong in general. It is wrong when the primary consumer is someone reasoning about one behavior at a time, which describes both a maintenance engineer and an AI agent editing code.

### Why this matters more for an agent

A human navigating a layered codebase builds a mental map over months. An agent starts from zero every session and discovers structure only from directory listings, file names, and imports. It has no memory of "the pricing logic is in `services/pricing_service.py`, which imports from `models/pricing.py`, which is fed by `adapters/price_adapter.py`".

A vertical slice makes the structure legible without prior knowledge. If the folder is named for the behavior and the entry point is declared in one file, an agent can answer "where does this live" from a single directory listing instead of a search. That is the entire design goal, and it is what the rules below enforce mechanically.

## 2. How this repository actually looks

Four real slices in `controller/`, verified against the tree:

```
controller/
├── pricing/          resolve model prices          __init__.py exports 7 names
├── fallbacks/        pick fallback models          __init__.py exports FallbackReconciler
├── sources/          enumerate model sources       __init__.py exports ModelSource
└── status/           poll and build status         __init__.py exports 7 names
```

`controller/pricing/` is the most complete example. Its public surface is declared in `__init__.py`:

```python
from .models import MatcherCandidate, PricingEntry, PricingResult
from .resolver import PricingResolver
from .source import PricingIndex, PricingSource
from .utils import pricing_to_params

__all__ = [
    "MatcherCandidate", "PricingEntry", "PricingIndex", "PricingResolver",
    "PricingResult", "PricingSource", "pricing_to_params",
]
```

Inside, the files are split by what each does, and none is large:

| File | Lines | Role |
|---|---|---|
| `source.py` | 184 | fetch and index price data |
| `matchers.py` | 149 | match a model name to an entry |
| `resolver.py` | 63 | orchestrate matching into a result |
| `aggregator.py` | 58 | combine multiple entries |
| `normalizer.py` | 57 | normalize model names |
| `models.py` | 27 | data shapes owned by the slice |
| `utils.py` | 12 | one private conversion helper |

This is a slice done correctly: the largest file is 184 lines, every file is named for its job, and `utils.py` exists but is 12 lines and slice-private.

By contrast, `mcp-source-link/` was until recently 18 Go files in a single flat package, 4003 lines total, separated only by filename prefixes (`tunnel.go`, `tunnel_connector.go`, `tunnel_probe_test.go`, `reverse.go`, `reverse_handlers.go`, `pool.go`, `probe.go`). The prefixes were doing the work a directory should do: `tunnel.go` mixed the member, the runtime, the gRPC server, and the probe loop, and finding "where is a request routed" meant knowing that `pool.go` held it. It has since been split into one package per behavior under `internal/`, which is the remedy this document describes. The lesson survives the fix: when a folder passes roughly ten files, an agent must read file names carefully to reconstruct the grouping, and that is the signal to cut slices.

## 3. The rules

### Rule 1 — One folder per behavior, named with a domain noun

A folder name must state a behavior. `pricing`, `fallbacks`, `status`, `sources` pass. `utils`, `common`, `core`, `helpers`, `misc` fail, because they name a technical role and therefore accept anything, which is how they become dumping grounds.

Test: read the folder name out loud. If it answers "what does this do", it passes. If it answers "what kind of code is this", it fails.

### Rule 2 — One public entry point per slice, declared in `__init__.py`

The `__init__.py` is the contract. Everything not in `__all__` is private and free to change. Callers use `from controller.pricing import PricingResolver`, never `from controller.pricing.resolver import PricingResolver`.

`fallbacks/__init__.py` is exactly right at three lines:

```python
from .service import FallbackReconciler

__all__ = ["FallbackReconciler"]
```

Small public surfaces are a feature. A slice exporting 30 names has not decided what it is for.

### Rule 3 — Layering inside a slice stops at four files

When a slice needs internal structure, use predictable names so the layout can be guessed without listing the directory:

```
pricing/
├── __init__.py    contract only
├── models.py      data shapes owned by this slice
├── service.py     the logic, one entry class or function
└── <detail>.py    named for what it does
```

`pricing/` uses seven files, which is the practical ceiling. Past that, the slice is usually two behaviors sharing a name and should be split.

`utils.py` inside a slice is acceptable **only** if small and slice-private. `pricing/utils.py` at 12 lines qualifies. A top-level `utils.py` never qualifies, because "top-level" means "usable by everything", which is the opposite of a slice boundary.

### Rule 4 — Dependencies point inward, and slices never import each other's internals

Slices may import shared infrastructure. Shared infrastructure may not import slices. Two slices may not import each other's private modules.

This repository has a **real violation** in `controller/controller.py`:

```python
from .fallbacks import FallbackReconciler        # fine, public surface
from .fallbacks.client import FallbackClient     # leak, slice internal
from .pricing import PricingResolver, ...        # fine, public surface
from .sources import ModelSource                 # fine, public surface
from .sources.local_deployments import LocalDeploymentSource    # leak
from .sources.submariner_imports import SubmarinerImportSource  # leak
```

`FallbackClient`, `LocalDeploymentSource`, and `SubmarinerImportSource` are not in their slices' `__all__`. Importing them by module path makes those classes de facto public while the contract claims otherwise. Moving `local_deployments.py` renames an API. The fix is one line per slice: add the names to `__all__` and import them from the package.

The same pattern appears two more times, in shared modules rather than the orchestrator: `controller/reconciler.py` and `controller/status_persister.py` both import `from .status.snapshot import OicmStatusSnapshot`, bypassing the `status` slice's public surface. Use this to find such leaks:

```bash
grep -rn 'from \.\(fallbacks\|sources\|status\|pricing\)\.' --include=*.py .
```

Five hits exist today: three in `controller.py` and one each in `reconciler.py` and `status_persister.py`. Any hit whose module is not `__init__` is a leak.

### Rule 5 — Shared code is small, boring, and dependency-free

Genuinely shared code, used by three or more slices, lives outside all slices. In `controller/` that is `config.py`, `models.py`, `litellm_client.py`, `reconciler.py`, and `controller.py` itself.

Rules for it:

- **No business decisions.** If it branches on domain concepts, it belongs in a slice.
- **Promote on the third use, not the second.** Two slices needing similar logic should duplicate it. Duplication is visible and cheap to remove later; premature sharing couples them permanently.
- **Never import a slice.** Enforce by keeping shared modules free of slice imports, which the command in Rule 4 makes checkable.

`controller/models.py` at 476 lines is the largest shared module and worth watching. It holds `OicmModel`, `strip_source_prefix`, and related shapes used by nearly every slice. It is currently justified as data shapes; if it accumulates behavior, that behavior belongs in slices.

### Rule 6 — Tests are co-located or mirrored, never mixed

Two acceptable layouts, chosen per repository.

**Co-located:**

```
pricing/
├── resolver.py
└── resolver_test.py
```

**Mirrored**, which is this repository's choice:

```
controller/pricing/resolver.py  ->  tests/controller/pricing/test_resolver.py
```

Mirroring is only valuable because the mapping is **derivable**: given a source path, the test path follows by rule. That derivability is what lets an agent find tests without searching. The moment a test file breaks the pattern, the property is gone.

**This repository breaks the mirror, and it has a cost.** Verified by comparing slice modules against existing test filenames:

| Slice module | Test file | Status |
|---|---|---|
| `pricing/source.py` | `tests/controller/pricing/test_source.py` | mapped |
| `pricing/resolver.py` | `tests/controller/pricing/test_resolver.py` | mapped |
| `pricing/aggregator.py` | `tests/controller/pricing/test_aggregator.py` | mapped |
| `pricing/matchers.py`, `normalizer.py` | matching files present | mapped |
| `status/`, 6 modules | only `test_status.py` at `tests/controller/` | **collapsed** |
| `fallbacks/service.py`, `client.py` | no file with either name | **absent** |
| `sources/base.py`, `submariner_imports.py` | no matching file | **absent** |

`pricing/` mirrors correctly and is the slice you can navigate confidently. `status/` has six modules tested by one file, so "where is `wire.py` tested" has no derivable answer. `fallbacks/` and parts of `sources/` have no directly mapped test at all.

The remedy is not to test every file. It is to make the mapping honest: either split `test_status.py` to match the modules it covers, or give the untested modules tests, or delete them if unused. `pricing/test_regression.py`, which has no matching module, is acceptable as an explicitly cross-cutting file, but it should be the acknowledged exception rather than an unnoticed pattern.

### Rule 7 — File size bounds are a slice signal, not a style rule

Bogard names the pattern's cost: it requires the ability to judge when a file is doing too much. A concrete bound makes that judgement mechanical.

- **Hard ceiling: 400 lines per file.** `controller/sources/local_deployments.py` at 364 lines is near it. `controller/models.py` at 476 lines already exceeds it.
- **Soft signal: any file over 200 lines** deserves a check that it is one behavior.

The bound is empirical: long files defeat both the reader's place-keeping and an agent's ability to hold a file in context. Lines are a proxy, but a reliable one.

### Rule 8 — Slice membership is decided by "what changes together"

The subtle rule, and the one that separates good slices from arbitrary ones. Group code that **changes for the same reason**.

Ask: "if this behavior changes, which files change?" The answer is the slice. If a change forces edits in two slices, the boundary is in the wrong place, or a shared module is missing.

Do not group by entity name alone. A `models/` folder holding one class per model is a data-layer folder, not a slice, because it changes for as many reasons as there are models.

## 4. Applying it

### Creating a new slice

1. **Name the behavior** with a domain noun. If no noun fits, the behavior is not yet understood.
2. **Create the folder** with `__init__.py`, `models.py` if it owns shapes, and one logic file named for the job.
3. **Write the logic first**, and add `__all__` once the surface is known. Do not export speculatively.
4. **Add the test file at the mirrored or co-located path** before wiring callers.
5. **Wire the caller to the public import only.**
6. **Run the test suite.** Confirm no import in the diff reaches past an `__init__.py`.

### Converting a layered folder

Reorganizing is riskier than greenfield. Sequence it so the tree keeps working:

1. **List files and group by behavior**, using domain keywords rather than reading each file top to bottom.
2. **Isolate genuinely shared files.** Anything used by three or more behaviors stays out of the slices.
3. **Move one slice at a time, with `git mv`.** Moving rather than copying makes broken imports fail loudly instead of silently duplicating.
4. **Add the `__init__.py` surface before touching callers**, so callers can switch to the public import in the same commit.
5. **Run tests after each slice move**, not after all of them. A failure then names one slice.
6. **Grep for the old paths** and confirm each import moved to a public surface rather than to a new internal path.

One slice per commit. A single reorganization commit touching thirty files cannot be reviewed and cannot be bisected if it breaks.

## 5. When not to use this

The pattern has real costs and does not fit every situation.

- **Very small codebases.** Under about a dozen files, folders are overhead. Wait until the layering starts hurting.
- **Genuinely cross-cutting infrastructure.** Logging, config loading, and the HTTP client are not slices and should not be forced into the shape.
- **Stable, shared, expensive abstractions.** If something truly is used identically everywhere and changes rarely, a layered shared module is the cheaper answer.
- **Teams without consistency in refactoring.** Bogard's own caveat: the pattern relies on people recognizing an overgrown slice. If that judgement is inconsistent, slices quietly become god folders.

If a slice grows a second behavior, split it. If two slices drift toward the same code, decide deliberately between duplicating and promoting on third use. Both are one-commit changes, which is the property the architecture is buying.

## 6. Done criteria

1. Every behavior has exactly one folder, named with a domain noun.
2. Every slice has `__init__.py` with an explicit `__all__`.
3. No import reaches into another slice's private module.
4. Shared modules import no slice.
5. Every source file's test is at a derivable path, or the file is explicitly acknowledged as untested.
6. No file exceeds 400 lines.
7. An agent can locate the implementation of any behavior from a single directory listing.

## Related

- [`additional-features-origin.md`](../../platform/additional-features-origin.md) — this fork's map of custom slices and where each lives
- [`logic_mapping_technique.md`](logic_mapping_technique.md) — trace-before-build, which precedes creating a slice
- [`code_smell_detection_technique.md`](code_smell_detection_technique.md) — detecting a slice that has grown too large or gone dead
- [Bogard, "Vertical Slice Architecture"](https://www.jimmybogard.com/vertical-slice-architecture/) — the original description and its stated costs
- [agents.md](https://agents.md/) — the open format for agent-facing instructions; nested `AGENTS.md` files per package complement this layout by putting local conventions next to the code they govern
