# Upstream Merge Technique — Reliable & Repeatable (v1.97.0 +)

> **Note on versions**: this doc was written during the v1.97.0 merge, so the
> worked examples reference v1.97.0 hashes and files. The sequence itself is
> version-independent: substitute the tag you are merging. Per-merge lessons
> live in their own sections at the bottom (v1.99.1 → v1.102.0 is there too).

> **Problem**: The OICM fork carries custom work on a long-lived branch
> (`jya0-v<X>.0`). The custom work is deliberately **refactored into co-located
> vertical slices** so upstream merges touch as few custom lines as possible. But
> every upstream release still produces a large set of conflicts, and a botched
> resolution silently drops a custom feature (a dropped argument, a deleted
> slice, a lost re-export) that only surfaces later in tests or at runtime.

> **Solution:** A deterministic sequence of git commands that (0) keeps
> regenerable `out/` artifacts out of the branch, (1) records the pre-merge
> SHAs and refreshes the staging branch from upstream if it still exists,
> (2) merges the upstream **tag directly into the existing custom branch while
> the custom branch is checked out** (never the reverse), (3) resolves each
> conflict by class, (4) verifies compile + drop-detection wiring, (5) audits
> files the merge deleted, (6) ratchets lint budgets on the custom branch,
> and (7) commits a proper two-parent merge commit (first parent = custom)
> and pushes.

---

## Merge direction (read this first)

The upstream merge runs **in the opposite direction** from a classic
"branch-off-tag" flow:

```
                       ef84494d (upstream tag v1.97.0)
                             \
              2690149502  ──── \  merge commit 6530e04544 ──▶ HEAD
              (ours: jya0-v1.97.0,  │
               custom branch)      /
                             ←  merge v1.97.0 INTO the custom branch
```

- The custom branch is **kept and rebased forward** by merging the new tag into it.
- The merge commit's **first parent is ours** (`2690149502`), the **second parent
  is the upstream tag** (`ef84494d`). Order matters: `--ours`/`--theirs` in
  conflict resolution are resolved relative to these parents.
- Do **not** delete the branch and recreate it from the tag. That loses the
  merge-parent history that lets CI and future merges see exactly what was custom.

**Correct — exactly this:**

```bash
git checkout jya0-v1.102.0        # the CUSTOM branch, checked out
git merge v1.102.0                # merge the upstream TAG into it
```

**Wrong — either of these produced the v1.102.0 clobber:**

```bash
git checkout -b tmp v1.102.0      # starting FROM the upstream tag ...
git merge jya0-v1.102.0           # ... then merging the custom branch in
```

...or deleting/recreating `jya0-v1.102.0` from the tag. Both put the upstream
commit in the first-parent slot: `--ours`/`--theirs` then mean the opposite of
what the resolver assumes, so every conflict resolved on autopilot takes the
wrong side (that is how the v1.102.0 merge clobbered hamsa's `api_surface` and
the UI Model Performance mounts; see the case study for why the OCR gate and
the 13 test files were lost regardless of direction).

Verify the direction at two moments — the commands differ because mid-merge
`HEAD` is still the pre-merge tip (a merge commit's parents do not exist until
you commit):

```bash
# While the merge is in progress (conflicts unresolved or resolved, NOT yet committed):
git rev-parse HEAD         # must equal the pre-merge custom tip ($OLD_CUSTOM_HEAD)
git rev-parse MERGE_HEAD   # must be the upstream tag

# After the merge commit exists:
git show --no-patch --format="%P" HEAD   # <ours> <upstream-tag>
git rev-parse HEAD^1                     # must print $OLD_CUSTOM_HEAD
```

First parent must be the **custom** tip and second the **upstream** tag. Use
`HEAD^1` for the post-commit check: `git merge-base HEAD <old-head>` always
prints `<old-head>` even when the direction is flipped, because both parents
are ancestors of the merge, so it proves nothing. If the first parent resolves
to an upstream/BerriAI commit, the direction flipped: stop and redo the merge
from the custom branch before resolving anything.

---

## Why the Merge Keeps Going Wrong

| Root Cause | Example | Step That Catches It |
|---|---|---|
| Piped `git merge` aborts via SIGPIPE | `git merge \| head` kills git mid-merge (exit 141; git rolls the whole merge back cleanly, but any resolution done before the abort is lost) | Step 2: never pipe a merge |
| `out/` re-imports every merge | Upstream keeps shipping new `litellm/proxy/_experimental/out/` files; a merge adds the ones that are new since last time even when the branch untracked them (173 were tracked again at `jya0-v1.102.0` HEAD after the v1.102.0 merge) | Step 3: bulk `git rm` + re-commit the untrack |
| `git checkout --ours` on a modify/delete conflict | Out/ paths have no "ours" stage, so it errors with "does not have our version"; `git rm` is the resolution | Step 3: resolve by conflict class |
| Took the wrong side of a shared-file conflict | Dropped `Final:` annotation, dropped a custom handler block | Step 4: py_compile + re-diff |
| Multi-line edit lost leading indentation | `Final:` annotation re-edit dropped 8/12 spaces | Step 4: py_compile every resolved file |
| Slice wiring silently dropped by the merge | Mount line / re-export / callback registration deleted | Step 4: `test_oicm_drop_detection.py` |
| Upstream deletes a file the branch still carries | Upstream's `test: drop static cost-map value assertions` removed 13 test/source paths; any merge (even correctly directioned) deletes them without a conflict | Step 5: deletion audit |
| Custom-only file deleted outright | Merge removed a file that existed only on the custom side, or one carrying custom edits | Step 5: drop audit |
| Lint budget left stale after merge | Merge adds errors but budgets weren't ratcheted; no automation ratchets this branch | Step 6: `make lint-budget-update` |
| **`uv.lock` reverted as a "local artifact"** | `uv.lock` carries **OICM-only deps** (`openrouter`, and its transitive `jsonpath-python`); reverting it silently drops them → Docker `uv sync --frozen` can't install → `ModuleNotFoundError: openrouter` at runtime | Step 4 / deploy smoke test |

---

## The Sequence

Set these once before starting; every later step references them:

```bash
NEW_TAG=v1.103.0                                          # upstream tag being merged in
CUSTOM_BRANCH=jya0-v1.102.0                               # the custom branch merging INTO
git fetch upstream --tags
OLD_CUSTOM_HEAD=$(git rev-parse $CUSTOM_BRANCH)           # pre-merge custom tip
UPSTREAM_BASE=$(git merge-base $CUSTOM_BRANCH $NEW_TAG)   # common ancestor
```

`OLD_CUSTOM_HEAD` and `UPSTREAM_BASE` are inputs to the Step 5 drop audit and
cannot be reconstructed reliably after the merge. Record them (or keep the
terminal open) before going further.

### Step 0 — Make generated `out/` trivially resolvable (commit once; re-apply after every merge)

Upstream tracks `litellm/proxy/_experimental/out/`; the custom branch
gitignores it. Without an explicit deletion on our side, every merge floods
400+ conflicts that are *content* conflicts — all three stages present, the
worktree littered with `<<<<<<<` markers — and each one looks like it needs a
manual resolution. After a deliberate untrack commit, our side has an explicit
deletion, so the same paths come back each merge as *modify/delete* conflicts
(only stages 1 and 3, no "ours" content) that one bulk `git rm` clears:

```bash
git rm -r --cached litellm/proxy/_experimental/out
git commit -m "chore: untrack generated litellm/proxy/_experimental/out build artifacts"
```

What this commit does **not** do (both were believed at some point, both are
wrong):

- It does **not** remove the out/ conflicts from later merges. The v1.97.0
  merge still hit 489 out/ paths (518 conflicted paths total) and the v1.99.1
  merge still hit 442 — all as modify/delete, all cleared by `git rm` (Step 3).
- It does **not** make out/ paths "resolve as take-ours automatically". They
  conflict every single merge and `git checkout --ours` **fails** on them with
  `error: path ... does not have our version`, because a modify/delete
  conflict has no stage-2 entry. `git rm` is the resolution.

> **Re-import trap:** the merge silently *re-adds* every out/ path that is new
> since the previous merge (upstream keeps shipping fresh build artifacts).
> After the v1.102.0 merge, 173 out/ files were tracked again on a branch that
> had an untrack commit. Re-run the `git rm -r --cached` after resolving and
> fold it into the merge commit, or out/ creeps back one merge at a time.

> Rule of thumb: if a conflict is a **regenerable artifact** (build output,
> generated code), resolve for deletion (`git rm`) and move on. If it is **real
> source**, it needs a real resolution.
>
> **⚠️ Lockfile exception — `uv.lock` is NOT safe to revert.** Unlike `out/`, the
> OICM branch's `uv.lock` deliberately **adds** dependencies that upstream does
> not ship: `openrouter` (which pulls in `jsonpath-python` transitively) and a
> `pydantic` pin that tracks what the installed openrouter SDK requires. The
> merge can show `uv.lock` as a conflict or an "unintended local artifact".
> **Do NOT revert it.** Taking the upstream version (or deleting it) silently
> drops the OICM deps; the Docker `uv sync --frozen` build then fails or
> produces an image that crashes with `ModuleNotFoundError: No module named
> 'openrouter'`. Keep the OICM lockfile; if it must be regenerated, merge the
> OICM deps back in and confirm all three are present before committing:
> `openrouter` (currently 1.3.22; pyproject pins `>=1.2.0,<2.0`),
> `jsonpath-python` (1.1.6), `pydantic` (2.12.5). These move: check
> `uv.lock` + `pyproject.toml` at the branch tip for the current values
> instead of trusting the numbers printed here.

### Step 1 — Refresh `litellm_internal_staging` from upstream (only if the ref still exists)

```bash
git fetch upstream --tags
git checkout litellm_internal_staging
git merge --ff-only upstream/litellm_internal_staging
```

`upstream/litellm_internal_staging` existed when this sequence was written but
**BerriAI has since deleted it** (archived locally as tag
`archive/litellm_internal_staging`; `git fetch upstream litellm_internal_staging`
fails with `couldn't find remote ref`). If the remote-tracking ref is gone,
skip the ff-only refresh — the local/origin `litellm_internal_staging` branch
is still an ancestor of `upstream/main` and purely a convenience mirror, not an
input the merge depends on. Verify before relying on it:

```bash
git rev-parse -q --verify upstream/litellm_internal_staging \
  || echo "ref gone upstream; skip the staging refresh"
```

### Step 2 — Merge the target tag into the custom branch

```bash
git checkout $CUSTOM_BRANCH
git merge $NEW_TAG         # merge the TAG in; ours = $CUSTOM_BRANCH
```

While the merge is in progress (conflicts present), confirm the direction has
not flipped — `HEAD` must still be the custom tip and `MERGE_HEAD` the tag:

```bash
git rev-parse HEAD         # must equal $OLD_CUSTOM_HEAD
git rev-parse MERGE_HEAD   # must be the upstream tag
```

If either is wrong, `git merge --abort` and restart from `$CUSTOM_BRANCH`.
Resolving a flipped merge inverts `--ours`/`--theirs`, so conflicts resolved on
autopilot take the wrong side — that is what clobbered three custom features in
v1.102.0 (see the case study at the bottom).

> **Trap — never pipe a `git merge`.** `git merge $NEW_TAG | head` kills git with
> SIGPIPE (exit 141) and git rolls the whole merge back: `MERGE_HEAD` gone,
> index clean. That is safer than it sounds, but any resolution you already made
> in that invocation is lost and the merge must restart from zero. If you must
> view partial output, redirect to a file:
> `git merge $NEW_TAG > /tmp/merge.log 2>&1`.

> Expect hundreds of conflicts even with the Step 0 untrack commit. The out/
> paths still conflict (as modify/delete — our side deleted them, upstream
> regenerated them); one bulk `git rm` in Step 3 clears all of them. What is
> left is the real set: 29 unique paths in v1.97.0 and in v1.99.1, of which
> roughly 20 are hand-resolved source files and the rest are budgets,
> `.gitignore`, `pyproject.toml`, `uv.lock`, and UI lock/build artifacts.

### Step 3 — Resolve conflicts by class

Get the full list first:

```bash
git status --short
git ls-files -u | wc -l          # unmerged count — should reach 0
git diff --check                 # catch stray whitespace / conflict markers
```

Classify each conflict:

- **Generated `out/` artifacts (modify/delete)** — our side deleted them, so
  there is no "ours" content and `git checkout --ours -- <path>` **errors**
  with `path ... does not have our version`. Resolve for deletion instead:
  ```bash
  git ls-files -u | awk '{print $4}' | grep '_experimental/out/' \
    | sort -u | xargs git rm -q --
  ```
  (489 paths in v1.97.0, 442 in v1.99.1. After resolving, note the merge has
  also *staged additions* of out/ paths that are new upstream — end Step 3 by
  re-running `git rm -r --cached litellm/proxy/_experimental/out` so they do
  not creep back into the tree; see the re-import trap in Step 0.)
- **Lint budget files** (`basedpyright-code-budget.json`,
  `ruff-strict-budget.json`, `type-discipline-budget.json`,
  `test-quality-budget.json`) — take ours, then ratchet in Step 6. Taking ours
  keeps the ceilings the branch already earned; the ratchet is a separate,
  deliberate commit.
- **Real source files** — merge line by line, **always keeping the OICM custom
  logic** and carrying the upstream structural/typing changes across. The full
  list of custom-kept resolutions in v1.97.0 (paths as they were at that
  merge; several have since moved upstream — e.g. `dual_cache.py` is now
  `litellm/caching/dual_cache.py`, `user_api_key_cache.py` is now
  `litellm/proxy/common_utils/user_api_key_cache.py`,
  `model_rate_limit_check.py` is now
  `litellm/router_utils/pre_call_checks/model_rate_limit_check.py` — the table
  is a historical record, not a current path map):

  | File | What OICM kept vs what upstream changed |
  |---|---|
  | `dual_cache.py` | kept `and not effective_skip` (cross-pod staleness fix) + upstream `Final:` |
  | `prometheus.py` | kept OICM `llm_provider` fallback to `custom_llm_provider` |
  | `prometheus_api.py` | kept OICM `Optional`/`Dict`/`timezone` imports + upstream `Final:` |
  | `litellm_logging.py` | kept OICM `dynamic_rate_limiter_v3_htb` handler block |
  | `llm_http_handler.py` | kept `Dict` (still used) + upstream `Final:` |
  | `main.py` | dropped unused `Dict`, took upstream `Final` import |
  | `_types.py` | kept `List`/`Optional`/`Union` (all used) + upstream `Final:` |
  | `handle_jwt.py` | kept `skip_in_memory=False` in all 3 cache calls + upstream `Final:` |
  | `user_api_key_cache.py` | kept `skip_in_memory=skip_in_memory` arg + upstream `Final:` |
  | `db_spend_update_writer.py` | kept `Dict`/`List`/`Optional` + upstream `MappingProxyType`/`Final:` |
  | `redis_update_buffer.py` | kept `Dict`/`Optional` + upstream `Final:` |
  | `_health_endpoints.py` | dropped unused `Union`; took upstream version |
  | `litellm_pre_call_utils.py` | added upstream `Mapping` import (used 4x) |
  | `team_endpoints.py` | kept BOTH `team_cache_invalidation` (OICM) + `team_metadata_validation` (upstream) |
  | `proxy_server.py` | kept OICM `update_model_performance_rollup` scheduler + upstream SGR `flush_gateway_requests` job |
  | `utils.py` | took upstream `valid_fallback_types: Final = [...]` (list) + `Final:` |
  | `router.py` | took upstream `voice: str` (no `= None` default), `model_name: Final:` |
  | `model_rate_limit_check.py` | kept OICM `htb_priority` check (2 blocks) |
  | `test_user_api_key_auth.py` | kept BOTH OICM team-cache test AND upstream JWT tests |

> The dominant theme: **upstream adds `Final:` annotations and structural typing
> changes; OICM adds custom behavior (staleness fixes, handlers, scheduler jobs,
> cache invalidation).** Resolution = keep the custom logic, adopt the upstream
> typing.

### Step 4 — Verify: compile, markers, drop-detection

After resolution:

```bash
# Capture the resolved file list BEFORE clearing the unmerged index —
# `git ls-files -u` prints nothing once every conflict is resolved, so
# deriving the list inside the loop is a no-op at that point.
git ls-files -u | awk '{print $4}' | sort -u > /tmp/resolved-files.txt

# Every resolved file must compile (catches lost indentation from re-edits)
while read -r f; do python3 -m py_compile "$f"; done < /tmp/resolved-files.txt

# No conflict markers / whitespace
git diff --check

# Import chain intact
python -c "import litellm"
python -c "from litellm.proxy import proxy_server"

# Slice wiring intact (the merge-specific safety net)
python -m pytest tests/test_litellm/proxy/test_oicm_drop_detection.py -q
```

> **Safety-net caveat:** drop-detection covers the *wired* slices (voice
> routes mount, HTB callback registration, provider dispatch, team-cache
> invalidation, prometheus ledger). It does not cover hamsa's `api_surface`
> passthrough kwargs, the OCR `_RUST_OCR_PROVIDERS` gate, or the UI Model
> Performance mounts — the v1.102.0 merge clobbered all three without failing
> a single test. Wiring that lives outside `test_oicm_drop_detection.py` is
> unprotected; check the merge diff for it manually, and add a drop-detection
> case when you add a slice.

> **Lost-indentation trap:** multi-line edit tools often trim the leading spaces
> off the first line of the replaced block, producing a silent `IndentationError`.
> This bit ~10 files in v1.97.0 (`dual_cache.py`, `prometheus.py` x2,
> `prometheus_api.py`, `handle_jwt.py` x2, `user_api_key_cache.py` x2,
> `proxy.py`, `router.py`). **Always run `py_compile` on every touched file.**

### Step 5 — Drop audit: files the merge deleted (mandatory post-merge)

Conflict resolution cannot catch whole files the merge removed without
conflicting — either because both sides touched the same paths, or because
upstream deleted a file our side never modified (git resolves that silently
to "deleted" in *any* merge direction; a rehearsal of the v1.102.0 pair in
the correct direction deletes the same 13 files the flipped merge did). Run
all three checks before committing (they also work after the merge commit by
adding `HEAD` to the diff):

```bash
# 1. Every file that exists pre-merge but is gone post-merge — the raw list
git diff --name-status $OLD_CUSTOM_HEAD -- litellm/ tests/ ui/ oicm-litellm-layer/ \
  | grep '^D' | cut -f2 > /tmp/deleted.txt

# 2. Which of those never existed upstream (pure custom additions)
while read -r f; do
  git cat-file -e "$UPSTREAM_BASE:$f" 2>/dev/null || echo "CUSTOM-ONLY FILE DELETED: $f"
done < /tmp/deleted.txt

# 3. Which of those carried custom edits over the base
while read -r f; do
  git cat-file -e "$UPSTREAM_BASE:$f" 2>/dev/null || continue
  git diff --quiet $UPSTREAM_BASE $OLD_CUSTOM_HEAD -- "$f" || \
    echo "DELETED WITH CUSTOM EDITS: $f"
done < /tmp/deleted.txt
```

**Check 1 is the one that matters and must not be skipped.** It was
check-1-style listing that surfaced the 13 files lost in v1.102.0 — checks 2
and 3 print nothing for them, because those files were upstream-authored,
upstream-deleted, and byte-identical on our side. If you only run 2 and 3 you
will conclude "nothing was lost" exactly when everything was. Every file from
check 1 that you do not deliberately accept needs a classification before you
act:

- **Subject still exists** (the feature, pricing entry, or helper the test
  imports survived) → restore from `$OLD_CUSTOM_HEAD`
  (`git checkout $OLD_CUSTOM_HEAD -- <path>`), adapting imports for any module
  that moved.
- **Upstream superseded it** (module renamed/restructured, replacement covers
  the same assertions) → do **not** restore; note the replacement in the merge
  PR so the deletion is a decision, not an accident.

Blind-restoring a superseded test fails against current behavior; several of
the 13 v1.102.0 losses were in that class (see Lessons Learned at the bottom).
That audit restored five (their subjects still exist), retired the embedding
`extra_body` patch, and deliberately dropped the rest as superseded — the
decision record is commit `104fcbcf2a` and its CHANGELOG entry.

### Step 6 — Lint budget ratchet (mandatory post-merge on the custom branch)

`CLAUDE.md` forbids editing or committing the four `*-budget.json` files on a
**PR branch** and forbids running `make lint-budget-update` there, because a
hosted Devin automation ratchets the repository default branch in its own PR
and concurrent PRs would otherwise fight over the same `"limit"` lines. That
rule does not cover this workflow: the `jya0-v<X>.0` branch is long-lived, work
lands on it as direct commits (no PR merge commits authored by us), and the
automation targets `litellm_internal_staging` on the fork, not this branch. No
automation has ever ratcheted a budget here, so if this step is skipped the
ceilings never come down.

Check whether a ratchet is actually due before running it. The gates measure
head violations against `merge-base($BASE_REF, HEAD)`, so a merge that already
landed upstream counts as a base shift rather than as head improvement:

```bash
git log --oneline -3 -- '*budget.json'    # last ratchet commit
BASE=$(python3 scripts/default_branch.py --base '')
git diff --stat "$(git merge-base "$BASE" HEAD)" HEAD -- '*budget.json'
```

Run once on a clean working tree:

```bash
make lint-budget-update
```

This runs four gates (the basedpyright one takes ~8 minutes; it provisions a
`.venv-typecheck` and runs head+base passes):
- `make lint-ruff-budget-update` (ruff-strict)
- `make lint-type-discipline-budget-update` (type-discipline)
- `make lint-test-quality-budget-update` (test-quality)
- `make lint-basedpyright-budget-update` (basedpyright, slowest)

Each reports a ratchet, e.g. "Ratcheted basedpyright limits down by 2416 errors
this branch fixed across 48 rules". Commit the budgets separately:

```bash
git add ruff-strict-budget.json type-discipline-budget.json \
  test-quality-budget.json basedpyright-code-budget.json
git commit -m "chore(lint): ratchet budgets after v1.102.0 merge"
```

> **The ratchet can write limits below the current count. Read the diff.**
> Every gate's `ratcheted_budget` is
> `max(0, limit - max(0, base - current))`, which assumes `limit >= base`.
> On this branch that assumption fails: the merge-base with the fork default is
> ~7200 commits back, so `base` is often far above the stored `limit`, and
> subtracting the whole delta overshoots past `current`. Measured on the
> v1.102.0 tip, one ruff-strict run takes `ANN401` from `1156` to `434` while
> head has `573`, and a second run takes it to `0`. Breaching rules go 22 to 26
> after one run and 33 after two, so the ratchet is not idempotent and repeated
> runs destroy the budget. It does not turn the gate red immediately, because
> `evaluate` also requires `total > base`, and the stale base hides the
> overshoot. It corrupts the next merge's baseline instead.
>
> After any ratchet, diff every rule against the live count and restore the
> ones that landed low:
>
> ```bash
> git diff -- ruff-strict-budget.json
> PATH="$PWD/.venv/bin:$PATH" ruff check litellm --config ruff-strict.toml \
>   --output-format json | python3 -c 'import json,sys,collections; \
> c=collections.Counter(i["code"] for i in json.load(sys.stdin)); \
> [print(f"{k} head={v}") for k,v in sorted(c.items())]'
> ```
>
> A correct ratchet is one run, reviewed, with no limit below head.
> Do not run it twice.

CI enforces the ratchet direction separately and non-gating:
`scripts/budget_ratchet_check.py` runs in `test-linting.yml` and turns red if
any `limit` rises, a rule is dropped, or a budget file is deleted. It is
deliberately not a required check, so a justified bump can still merge once a
human accepts the red.

> **Budget gotcha:** if the working tree contains unrelated changes (e.g. an
> uncommitted image-tag bump), the ratchet measures them too. Commit the merge
> resolution to a clean tree, or stash the unrelated change, before running.

> **State at the v1.102.0 tip:** the last ratchet is `ec2070034e` (2026-09-02,
> after the v1.99.1 merge). The v1.102.0 merge itself preserved the custom
> budgets byte for byte and no ratchet followed it, so one is overdue.
>
> The ruff-strict gate is already red at this tip: 22 rules sit above their
> stored limit (largest gaps `TID251` 1126 over 0, `F401` 281 over 23, `RET504`
> 175 over 0, `RUF012` 240 over 168). That is a pre-existing state, not a
> ratchet result, and it means a passing gate is not the bar for Step 6. Do not
> try to "fix" it by ratcheting: a ratchet only lowers ceilings, it never
> removes violations, and here it would push several limits further below head.
> Clear the violations or accept the red explicitly.

### Step 7 — Commit the merge & push

```bash
# Re-apply a stashed debug-manifest image-tag bump if any
git stash pop            # e.g. 'bump debug manifest to jya0-v1.102.0'

# Commit the merge (reuses the auto-generated message)
git add -A
git commit --no-edit     # parents: <ours> <upstream-tag>

git push origin $CUSTOM_BRANCH
```

Verify the final state — the parent order one last time:

```bash
git show --no-patch --format="%P" HEAD    # first parent MUST be $OLD_CUSTOM_HEAD
git rev-parse HEAD^1                      # same check, machine-comparable
git log --oneline -5                      # merge, then budget/manifest commits
git rev-parse HEAD origin/$CUSTOM_BRANCH  # must be identical (fully pushed)
git status --short                        # clean tree
```

If `HEAD^1` is not `$OLD_CUSTOM_HEAD`, the direction flipped; see the
Merge direction section above and redo before pushing.

---

## Distinguishing Merge Regressions from Pre-existing Failures

A failing test in the merged tree is not automatically your fault. Confirm it is
a regression you introduced before hunting it:

```bash
# baseline A: does it fail on the tag you merged from?
git worktree add /tmp/base $NEW_TAG
cd /tmp/base && python -m pytest <test> -q

# baseline B: does it fail on the pre-merge custom branch?
git worktree add /tmp/old $OLD_CUSTOM_HEAD
cd /tmp/old && python -m pytest <test> -q

git worktree remove /tmp/base --force && git worktree remove /tmp/old --force && git worktree prune
```

- Fails on neither baseline → merge regression, fix it.
- Fails on both → pre-existing (often a brittle test). Not a merge blocker.

---

## Lessons Learned (v1.97.0)

- **The out/ untrack commit changes the conflict *class*, not the count.**
  It does not stop the conflicts (489 out/ paths conflicted in v1.97.0 and
  442 in v1.99.1), but it turns them from content conflicts with markers
  into modify/delete conflicts that one bulk `git rm` clears. It also must be
  re-applied after every merge: each merge re-imports the out/ paths that are
  new upstream.
- **Merge INTO the custom branch**, keeping first-parent = ours. Recreating from
  the tag loses the merge-parent lineage.
- **Never pipe a merge.** SIGPIPE (exit 141) makes git roll the merge back
  cleanly, losing any resolution already done in that invocation.
- **Keep custom logic, adopt upstream typing.** The dominant conflict pattern is
  upstream adding `Final:`/typing discipline while OICM carries concrete logic.
- **`py_compile` every touched file.** Silent indentation loss from multi-line
  edits is the top source of "compiles upstream but not after the merge". Build
  the file list *before* resolving the last conflict; `git ls-files -u` is
  empty once the index is clean.
- **Ratchet the lint budgets on the custom branch.** Four separate budget
  files; run `make lint-budget-update` on a clean tree. The `CLAUDE.md` ban on
  budget edits targets PR branches because a hosted automation ratchets the
  repository default branch; that automation has never touched this branch, so
  skipping the ratchet here leaves the ceilings stale indefinitely.
- **Drop-detection tests are your safety net — with limits.** The OICM slice
  refactors are covered by `tests/test_litellm/proxy/test_oicm_drop_detection.py`;
  a future merge that drops a mount/re-export/callback fails a test instead of
  production. But it only covers the wired slices; kwargs lists and UI mounts
  have no drop-detection coverage and must be checked in the diff.
- **Production stays on the last known-good tag.** Merge and validate the debug
  gateway on the new image; never deploy the merged image to production.
- **Never revert a lockfile that adds an OICM dependency.** `uv.lock` is a
  first-class part of the OICM diff, not a local artifact. Reverting it in a
  merge drops `openrouter` (and its transitive `jsonpath-python`) and unpins
  the `pydantic` the SDK needs, so the image fails to import `openrouter_compat`
  at startup. If the lockfile looks "wrong" after a merge, diff it against
  the pre-merge branch (`git diff <old-branch> -- uv.lock`) before touching it;
  the extra packages are intentional OICM additions. The versions move over
  time (openrouter is past 1.3.x now, pydantic pin tracked the SDK
  requirement) — read the current pins out of `uv.lock`/`pyproject.toml` at
  the branch tip, not out of this doc.
- **Smoke-test the image, not just the tree.** `python -c "import litellm"` and
  the drop-detection tests prove the Python tree, but they do not prove the
  **container** has the right deps. After building, verify the merged image
  actually boots the debug gateway and serves a model request with the master
  key before considering the merge done. The `openrouter` crash only surfaced at
  gateway startup, not in any import test.

---

## How the v1.102.0 merge went wrong (case study)

The v1.99.1 → v1.102.0 merge was performed with the direction reversed: merge
commit `82e738ccf9` carries the upstream tag as **first** parent and the old
custom branch as second. What that actually cost, verified by re-running the
same merge pair in both directions in scratch worktrees, splits into two
distinct loss classes that need different fixes:

- **`--ours`/`--theirs` meant the opposite of what the resolver assumed.**
  Content conflicts resolved on autopilot took the wrong side. That is how
  hamsa's `api_surface` kwargs and the UI Model Performance mounts were
  clobbered — both files *conflict* in a correct-direction rehearsal, so a
  properly directioned merge forces a human decision exactly where the
  flipped merge let autopilot take the wrong side. The restore commits
  (`147a4e93b6` hamsa, `1b75323dcf` UI mounts) each fixed one of these, days
  later.
- **The OCR `_RUST_OCR_PROVIDERS` gate was lost in *either* direction.** Our
  `ocr/main.py` was byte-identical to the merge base; upstream's native
  lifecycle rework gutted the file (-705 lines) and removed the gate. Since
  our side never touched the file, git silently takes upstream's rewrite in
  the correct direction too. Restoring it (`031043b779`) was a deliberate
  re-port of custom behavior onto upstream's new structure — the class of
  loss the Step 5 audit + "subject still exists" classification exists for,
  not a direction failure.
- **The 13 lost test/source files were NOT caused by the flip.** All of them
  were upstream-authored, deleted upstream between the tags (e.g. `test: drop
  static cost-map value assertions`), and byte-identical on our side. A
  correct-direction rehearsal of the same pair deletes the exact same 13 files,
  silently and without conflict — that is plain git behavior for
  "upstream deleted, we never touched it". The flip made them *easier to
  miss* (they hid among ~280 staged deletions of out/ churn), but the lesson
  is that any merge, done perfectly, still needs the Step 5 deletion audit.

## Lessons Learned (v1.99.1 → v1.102.0)

- **Direction violation is recoverable but expensive.** The flipped merge
  landed upstream-first; `--ours`/`--theirs` then meant the opposite of what
  the resolver assumed, and every conflict resolved on autopilot took the
  wrong side.
- **"Upstream deleted it, we never modified it" is invisible in every
  direction.** The file losses that prompted the Step 5 audit were not flip
  artifacts at all; git silently takes upstream's deletion whether the merge
  is directioned correctly or not. The audit that matters is the *raw
  deletion list* (Step 5, check 1), not just the custom-only/custom-edit
  subclasses (checks 2 and 3), which print nothing for this loss class.
- **A silent deletion still shows in `git status`** as a staged `D` — but only
  if someone reads it. It drowns in the ~280 out/ deletions staged at the
  same time. Bulk-resolve out/ first, then read what is left.
- **Superseded custom tests should be dropped, not restored.** Upstream rewrote
  `test_tool_call_streaming_transformation.py` (fc_ item-id prefix behavior),
  the rust OCR bridge (lifecycle/configuration split), and the autoroute
  settings module (merge_claude_settings refactor); restoring the old tests
  against the new code fails. Verify coverage exists in the upstream
  replacement first. The final classification lives in commit `104fcbcf2a`:
  five restored, the rest accepted as superseded.
- **Embedding `extra_body` patch is retired.** Upstream now passes vLLM
  embedding `extra_body` through `get_optional_params_embeddings` natively;
  `patches/embedding-extra-body.patch` no longer applies and was removed.
- **Re-verify every post-merge restore commit.** The three "fix(...): restore"
  commits after this merge were all merge clobbers found days later in
  production or code review. The Step 5 drop audit exists so the next merge
  finds them on merge day.
