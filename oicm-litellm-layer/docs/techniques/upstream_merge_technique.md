# Upstream Merge Technique — Reliable & Repeatable (v1.97.0 +)

> **Problem**: The OICM fork carries custom work on a long-lived branch
> (`jya0-v<X>.0`). The custom work is deliberately **refactored into co-located
> vertical slices** so upstream merges touch as few custom lines as possible. But
> every upstream release still produces a large set of conflicts, and a botched
> resolution silently drops a custom feature (a dropped argument, a deleted
> slice, a lost re-export) that only surfaces later in tests or at runtime.

> **Solution:** A deterministic sequence of git commands that (1) refreshes the
> staging branch from upstream, (2) merges the upstream **tag directly into the
> existing custom branch** (never the reverse), (3) resolves each conflict by
> class, (4) runs a drop-detection + lint-budget gate, and (5) commits a proper
> two-parent merge commit and pushes.

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

Verify the parents right after the merge resolves:

```bash
git show --no-patch --format="%P" HEAD   # <ours> <upstream-tag>
git merge-base HEAD <old-head>           # should == <old-head> (our side is linear)
```

---

## Why the Merge Keeps Going Wrong

| Root Cause | Example | Step That Catches It |
|---|---|---|
| Untracked generated files block the merge | `litellm/proxy/_experimental/out/` tracked upstream but gitignored locally | Step 0: deliberate untrack commit |
| Piped `git merge` aborts via SIGPIPE | `git merge \| head` kills git mid-merge | Step 2: never pipe a merge |
| Took the wrong side of a shared-file conflict | Dropped `Final:` annotation, dropped a custom handler block | Step 4: py_compile + re-diff |
| Multi-line edit lost leading indentation | `Final:` annotation re-edit dropped 8/12 spaces | Step 4: `py_compile` every resolved file |
| Slice wiring silently dropped by the merge | Mount line / re-export / callback registration deleted | Step 5: `test_oicm_drop_detection.py` |
| Lint budget left stale after merge | Merge adds errors but budgets weren't ratcheted | Step 5: `make lint-budget-update` |

---

## The Sequence

### Step 0 — Make generated `out/` unconflicted (do this once, per branch)

Upstream tracks `litellm/proxy/_experimental/out/`; the custom branch
gitignores it. Every merge then floods 400+ conflicts on regenerable artifacts.
Neutralize it **once per branch** with a deliberate untrack commit:

```bash
git rm -r --cached litellm/proxy/_experimental/out
git commit -m "chore: untrack generated litellm/proxy/_experimental/out build artifacts"
```

After this, those paths resolve as "take ours" (deletion) automatically on every
future merge. In the v1.97.0 merge this single commit removed **489 of the 518**
conflicts.

> Rule of thumb: if a conflict is a **regenerable artifact** (build output,
> generated code, lockfiles the tool manages), take ours and move on. If it is
> **real source**, it needs a real resolution.

### Step 1 — Refresh `litellm_internal_staging` from upstream

```bash
git fetch upstream
git checkout litellm_internal_staging
git merge --ff-only upstream/litellm_internal_staging
```

### Step 2 — Merge the target tag into the custom branch

```bash
git checkout jya0-v1.97.0
git merge v1.97.0          # merge the TAG in, ours = jya0-v1.97.0
```

> **Trap — never pipe a `git merge`.** `git merge v1.97.0 | tail` aborts the
> merge midway with a SIGPIPE, leaving a half-written index. If you must view
> partial output, redirect to a file: `git merge v1.97.0 > /tmp/merge.log 2>&1`.

> Expect hundreds of conflicts. The `out/` untrack commit (Step 0) turned the
> bulk into trivial take-ours deletions; the rest are the 22 real source files.

### Step 3 — Resolve conflicts by class

Get the full list first:

```bash
git status --short
git ls-files -u | wc -l          # unmerged count — should reach 0
git diff --check                 # catch stray whitespace / conflict markers
```

Classify each conflict:

- **Generated `out/` artifacts** — take ours (the branch keeps them untracked):
  ```bash
  git checkout --ours -- <path>
  ```
  This is the *deletion* side (they are untracked on our branch), so it removes
  the upstream file. Do this for all 489 `out/` paths.
- **Lint budget files** (`basedpyright-code-budget.json`,
  `ruff-strict-budget.json`, `type-discipline-budget.json`) — take ours, then
  ratchet in Step 5.
- **Real source files** — merge line by line, **always keeping the OICM custom
  logic** and carrying the upstream structural/typing changes across. The full
  list of custom-kept resolutions in v1.97.0:

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
# Every resolved file must compile (catches lost indentation from re-edits)
for f in $(git ls-files -u | awk '{print $4}'); do python3 -m py_compile "$f"; done

# No conflict markers / whitespace
git diff --check

# Import chain intact
python -c "import litellm"
python -c "from litellm.proxy import proxy_server"

# Slice wiring intact (the merge-specific safety net)
python -m pytest tests/test_litellm/proxy/test_oicm_drop_detection.py -q
```

> **Lost-indentation trap:** multi-line edit tools often trim the leading spaces
> off the first line of the replaced block, producing a silent `IndentationError`.
> This bit ~10 files in v1.97.0 (`dual_cache.py`, `prometheus.py` x2,
> `prometheus_api.py`, `handle_jwt.py` x2, `user_api_key_cache.py` x2,
> `proxy.py`, `router.py`). **Always run `py_compile` on every touched file.**

### Step 5 — Lint budget ratchet (mandatory post-merge)

Per `CLAUDE.md`, the three budget files are **ratcheted down** so lint ceilings
don't leave stale headroom after the merge. Run once on a clean working tree:

```bash
make lint-budget-update
```

This runs three gates (the basedpyright one takes ~8 minutes; it provisions a
`.venv-typecheck` and runs head+base passes):
- `make lint-ruff-budget-update` (ruff-strict)
- `make lint-type-discipline-budget-update` (type-discipline)
- `make lint-basedpyright-budget-update` (basedpyright — slowest)

Each reports a ratchet, e.g. "Ratcheted basedpyright limits down by 2416 errors
this branch fixed across 48 rules". Commit the budgets separately:

```bash
git add ruff-strict-budget.json type-discipline-budget.json basedpyright-code-budget.json
git commit -m "chore(lint): ratchet budgets after v1.97.0 merge"
```

> **Budget gotcha:** if the working tree contains unrelated changes (e.g. an
> uncommitted image-tag bump), the ratchet measures them too. Commit the merge
> resolution to a clean tree, or stash the unrelated change, before running.

### Step 6 — Re-apply manifest change, commit & push

```bash
# Re-apply a stashed debug-manifest image-tag bump if any
git stash pop            # e.g. 'bump debug manifest to jya0-v1.97.0'

# Commit the merge (reuses the auto-generated message)
git add -A
git commit --no-edit     # parents: <ours> <upstream-tag>

git push origin jya0-v1.97.0
```

Verify the final state:

```bash
git log --oneline -5                      # merge, then budget/manifest commits
git rev-parse HEAD origin/jya0-v1.97.0    # must be identical (fully pushed)
git status --short                        # clean tree
```

---

## Distinguishing Merge Regressions from Pre-existing Failures

A failing test in the merged tree is not automatically your fault. Confirm it is
a regression you introduced before hunting it:

```bash
# baseline A: does it fail on the tag you merged from?
git worktree add /tmp/base v1.97.0
cd /tmp/base && python -m pytest <test> -q

# baseline B: does it fail on the pre-merge custom branch?
git worktree add /tmp/old 2690149502
cd /tmp/old && python -m pytest <test> -q

git worktree remove /tmp/base --force && git worktree remove /tmp/old --force && git worktree prune
```

- Fails on neither baseline → merge regression, fix it.
- Fails on both → pre-existing (often a brittle test). Not a merge blocker.

---

## Lessons Learned (v1.97.0)

- **`out/` untrack commit is the single biggest lever.** It cut 518 conflicts to
  29 real. Do it once per branch and every later merge is cheap.
- **Merge INTO the custom branch**, keeping first-parent = ours. Recreating from
  the tag loses the merge-parent lineage.
- **Never pipe a merge.** SIGPIPE aborts git mid-merge.
- **Keep custom logic, adopt upstream typing.** The dominant conflict pattern is
  upstream adding `Final:`/typing discipline while OICM carries concrete logic.
- **`py_compile` every touched file.** Silent indentation loss from multi-line
  edits is the top source of "compiles upstream but not after the merge".
- **Ratchet the lint budgets.** Three separate budget files; run
  `make lint-budget-update` on a clean tree.
- **Drop-detection tests are your safety net.** The OICM slice refactors are
  covered by `tests/test_litellm/proxy/test_oicm_drop_detection.py`; a future
  merge that drops a mount/re-export/callback fails a test instead of production.
- **Production stays on the last known-good tag.** Merge and validate the debug
  gateway on the new image; never deploy the merged image to production.
