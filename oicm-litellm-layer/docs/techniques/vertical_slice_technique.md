# Vertical Slice Technique — Organizing Code So an Agent Can Read It At A Glance

Use this when creating a new feature area or reorganizing a folder whose files are grouped by technical layer (`models.py`, `utils.py`, `helpers.py` at the top) instead of by what the code does.

The goal is specific: **an agent or engineer should locate, read, and change one behavior by opening one folder.** Not by grepping, not by following imports across five directories.

## The principle

From [Jimmy Bogard's canonical statement](https://www.jimmybogard.com/vertical-slice-architecture/) of the pattern:

> Instead of coupling across a layer, we couple vertically along a slice. Minimize coupling between slices, and maximize coupling in a slice.

Three consequences that drive every rule below:

1. A slice owns everything needed to fulfill its behavior: entry point, logic, data shapes, and its tests.
2. Two slices may duplicate a small amount of code rather than share an abstraction. Duplication across a boundary is cheaper than coupling through one.
3. Shared infrastructure exists, but it is small, explicit, and contains no business logic.

Bogard also names the cost plainly: the pattern assumes the team can recognize when a file is doing too much. With small slices this is easy to check mechanically, which is what the rules below make possible.

## Rule 1 — One folder per behavior, named for the behavior

```
controller/
├── pricing/              ← one behavior: resolve prices
├── fallbacks/            ← one behavior: pick fallback models
├── sources/              ← one behavior: model sources
└── status/               ← one behavior: status polling
```

Not this:

```
controller/
├── models.py
├── utils.py
├── helpers.py
├── pricing_logic.py
└── fallback_handler.py
```

The test: **can you name the folder with a noun from the domain?** `pricing`, `fallbacks`, `status` pass. `utils`, `common`, `core`, `helpers` fail, because they name a technical role rather than a behavior, and every one of them becomes a dumping ground.

## Rule 2 — Every slice exposes exactly one public entry point

Each slice package has an `__init__.py` that exports its public surface and nothing else:

```python
# controller/pricing/__init__.py
from .resolver import PricingResolver

__all__ = ("PricingResolver",)
```

Callers import `from controller.pricing import PricingResolver`. They never import `controller.pricing.resolver`. This is what makes a slice's internals free to change: the `__init__.py` is the contract, the rest is private.

Concretely, that yields:

| Signal | Meaning |
|---|---|
| `from controller.pricing import X` | correct, uses the public surface |
| `from controller.pricing.resolver import X` | leak, the caller now depends on internals |
| `__all__` present | the slice declares its surface |
| `__all__` absent | the slice has no contract, fix it |

## Rule 3 — Layering inside a slice stops at four files

When a slice genuinely needs internal structure, use a fixed small set of names so an agent can predict the layout without listing the directory:

```
pricing/
├── __init__.py      ← public exports only
├── models.py        ← data shapes owned by this slice
├── service.py       ← the logic, one entry class or function
└── <detail>.py      ← named for what it does: resolver.py, matchers.py, normalizer.py
```

The real example in this repo, `controller/pricing/`, has `resolver.py`, `matchers.py`, `normalizer.py`, `aggregator.py`, `source.py`, `models.py`, `utils.py`. That is one slice spanning seven files, which is the upper limit; beyond it, the slice is usually two behaviors wearing one name.

**`utils.py` inside a slice is acceptable only if it is small and slice-private.** At the top level of a package, a `utils.py` is a warning sign.

## Rule 4 — Tests live beside the slice or in a mirrored path, never mixed

Two acceptable layouts. Pick one per repository and never mix.

**Co-located** (best for agent locality: one folder answers "how does this work" and "is it tested"):

```
pricing/
├── __init__.py
├── resolver.py
└── resolver_test.py
```

**Mirrored** (the convention in this repository):

```
controller/pricing/resolver.py
tests/controller/pricing/test_resolver.py
```

Mirroring works because the path is mechanically derivable: `X/y.py` maps to `tests/X/test_y.py`. That derivability is the whole value. If a test file breaks the mirror (`test_misc.py`, `test_utils.py`), an agent can no longer find the tests for a file by rule and must search.

## Rule 5 — Shared infrastructure is one folder, and it is boring

Anything genuinely used by three or more slices goes in one place. In this repository that is `controller/` level files (`config.py`, `models.py`, `litellm_client.py`) and the shared `_core`-style modules.

The rules for it:

- **No business logic.** If it makes a domain decision, it belongs in a slice.
- **Promote on the third use, never the second.** Two slices needing similar code is duplication; three is a shared module. Promoting early couples them for no benefit.
- **It may not import from any slice.** Dependencies point one way: slices import shared, shared never imports slices. This keeps shared code from becoming an accidental hub.

## Rule 6 — Dependencies point at interfaces, and slices never import each other

If slice A needs behavior from slice B, B exports an interface from its `__init__.py` and A imports that. An imported internal module is a coupling bug, not a style issue, because it makes B's internals part of A's contract.

For the controller pattern already in use here, this is why `sources/` defines a `ModelSource` ABC and `controller.py` orchestrates implementations without knowing their internals.

## Rule 7 — A slice must be readable top-down in under a minute

The acceptance test for agent comprehension, in order:

1. **Folder name states the behavior.** No decoding needed.
2. **`__init__.py` lists the public surface.** Read four lines, know what it offers.
3. **One file holds the primary logic**, and its name says what it is (`service.py`, `resolver.py`, `reconciler.py`).
4. **No file exceeds roughly 400 lines.** Beyond that, a reader loses place and an agent starts truncating.
5. **No import crosses into another slice's internals.**

If an agent asked "where is pricing resolution implemented" lands on `controller/pricing/resolver.py` in one directory listing, the slice is right. If it lands on `controller/pricing_logic.py` inside a folder of twelve similarly-suffixed files, it is not.

## Naming rules that make location deterministic

| Thing | Pattern | Good | Bad |
|---|---|---|---|
| Behavior folder | domain noun | `pricing/`, `fallbacks/` | `utils/`, `common/`, `core/` |
| Slice entry point | `service.py` or the behavior verb | `resolver.py`, `reconciler.py` | `main.py` inside a non-app package |
| Data shapes | `models.py` | `models.py` | `types.py`, `schema.py`, `dto.py` |
| Private detail | the thing it does | `matchers.py`, `normalizer.py` | `helpers.py`, `misc.py` |
| Test | `test_<module>.py` | `test_resolver.py` | `test_pricing.py` |

## Applying this to an existing messy folder

Reorganizing is riskier than greenfield, so sequence it to keep the tree working:

1. **List the files and group them by behavior**, not by type. `grep -l` on a domain term is usually faster than reading each file.
2. **Identify the files that belong to three or more behaviors.** Those are the shared candidates; everything else has one home.
3. **Create one slice folder at a time and move files in.** Move, do not copy, so imports fail loudly rather than silently duplicating.
4. **Add the `__init__.py` export surface** before updating callers, so callers can switch to the public import in the same commit.
5. **Run the test suite after each slice move**, not after all of them. A failure then points at one slice.
6. **Grep for the old paths** afterwards and confirm every import moved to the public surface.

Do one slice per commit. A single "reorganization" commit that touches thirty files cannot be reviewed, and if it breaks, it cannot be bisected.

## Done criteria

1. Every behavior has one folder, named with a domain noun.
2. Every slice has an `__init__.py` with `__all__`.
3. No caller imports a module inside another slice.
4. Shared code lives outside every slice and imports no slice.
5. Tests are co-located or mirrored, never mixed, and derivable by path.
6. No file exceeds roughly 400 lines.
7. An agent can find the implementation of any behavior from a single directory listing.

## Related

- [`additional-features-origin.md`](../additional-features-origin.md) — the map of this fork's custom slices and where each lives
- [`logic_mapping_technique.md`](logic_mapping_technique.md) — trace-before-build, the workflow that precedes creating a slice
- [`code_smell_detection_technique.md`](code_smell_detection_technique.md) — detecting a slice that has grown too large or gone dead
- [`AGENTS.md`](https://agents.md/) — the open format for agent-facing repository instructions; nested `AGENTS.md` files per package are the agent-native complement to this layout
