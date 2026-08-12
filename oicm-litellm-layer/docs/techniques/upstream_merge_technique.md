# Upstream Pull & Branch Merge Technique — Reliable & Repeatable

> **Problem**: Keeping a fork's custom work (carried on a dedicated branch) in
> sync with the latest upstream release involves multiple brittle manual steps:
> updating the staging branch from upstream, checking out a tag, creating a new
> branch, and merging the old custom branch into it. Skipping a step or mishandling
> a conflict produces a broken tree (dropped arguments, wrong file versions,
> leaked conflict markers) that surfaces only much later in tests or at runtime.

> **Solution**: A deterministic sequence of git commands that (1) refreshes the
> staging branch from upstream, (2) creates a fresh working branch from the target
> tag, (3) merges the old custom branch in, and (4) verifies the result with an
> explicit conflict/regression checklist before committing.

---

## Why the Merge Keeps Going Wrong

| Root Cause | Example | Step That Catches It |
|---|---|---|
| Staging is stale | Fast-forward blocked by untracked generated files | Step 1: fetch + `git clean` |
| No clean base | Branch created from a stale commit instead of the tag | Step 2: branch from tag |
| Blind conflict resolution | Took the wrong side, dropped a function argument | Step 4: dropped-argument audit |
| Merged tree silently broken | Custom HTB API removed by taking upstream's file | Step 4: dependency cross-check |
| Uncommitted merge state | Conflicts fixed but never committed, then lost | Step 5: commit promptly |

---

## The Sequence

### Step 1 — Refresh `litellm_internal_staging` from upstream

```bash
git fetch upstream
git checkout litellm_internal_staging
git merge --ff-only upstream/litellm_internal_staging
```

> **Trap**: if the staging branch is not cleanly fast-forwardable (e.g. generated
> files under `litellm/proxy/_experimental/out/` are gitignored locally but
> tracked by upstream), the FF is blocked by untracked files. Resolve with:
>
> ```bash
> git clean -fd   # only when you're certain the files are regenerable artifacts
> git merge upstream/litellm_internal_staging --ff-only
> ```

### Step 2 — Create the working branch off the target tag

```bash
git checkout <TAG>        # e.g. v1.96.2
git checkout -b jya0-<TAG>
```

> The new branch must come **directly from the tag**, never from the stale
> staging branch, so your custom work is rebased onto the exact release you want.

### Step 3 — Merge the old custom branch in

```bash
git merge <old-branch>    # e.g. jya0-v1.95.0
```

> Expect conflicts. They are the point of the exercise: the newer tag moved code
> while your custom branch added features on top of the older base.

---

## Step 4 — Conflict Resolution & Verification Checklist

This is where the technique earns its keep. Apply in order.

### 4.1 List all conflicted files

```bash
git status
git ls-files -u | wc -l          # count unmerged paths — should reach 0
git diff --check                 # catch stray whitespace / conflict markers
```

### 4.2 Classify each conflict

- **Custom feature that upstream does not have** (e.g. a custom HTB rate
  limiter). Upstream's "resolved" version of sibling files depends on it. Take
  **the whole custom file**:
  ```bash
  git checkout <old-branch> -- <path>
  ```
  but *only* after confirming the merged tree's unconflicted files reference the
  custom API (see 4.3).
- **Generated artifacts** (`out/`, build output). Always take **ours** (the tag):
  ```bash
  git checkout --ours -- <path>
  ```
- **Shared code** (`.gitignore`, typing imports, conditionals). Merge **line by
  line**, keeping the newer structure and carrying the custom logic fix across.

### 4.3 Dependency cross-check (critical)

If you took a whole custom file, verify the merged tree's other files that
*reference* it came from the custom branch too. Classic miss: the new branch
`refactored a call site but the merged tree kept it unchanged while you replaced
the callee with the new branch's version that uses a different signature.*

```bash
git diff <tag> <old-branch> -- <referencing_file> | head
```

> Concrete miss from the reference merge: `auth_checks.py` had a call
> `_cache_management_object(value=team_table, ...)`. During line-by-line merging
> the `value=` keyword argument was dropped, silently changing the call. The
> error surfaced only as 17 failing team-endpoint tests, not as a syntax error.

### 4.4 Dropped-argument audit

For every manually merged function, diff the resolved file against both sides and
confirm every positional/keyword argument survived:

```bash
git diff <tag> -- <file>                    # vs the new branch
git diff <old-branch> -- <file>             # vs your custom branch
```

### 4.5 Regression test sweep

Run the tests that map to every conflict-resolved module:

```bash
python -m pytest tests/test_litellm/<area>/test_<module>.py -q
```

### 4.6 Confirm import chain

```bash
python -c "import litellm"
python -c "from litellm.proxy import proxy_server"
```

---

## Distinguishing Merge Regressions from Pre-existing Failures

A failing test in the merged tree is **not automatically your fault**. Before
hunting a bug, confirm the failure is a regression you introduced:

```bash
# baseline A: does it fail on the tag you branched from?
git worktree add /tmp/base <tag>
cd /tmp/base && python -m pytest <test> -q

# baseline B: does it fail on the old custom branch?
git worktree add /tmp/old <old-branch>
cd /tmp/old && python -m pytest <test> -q
```

- Fails on **neither** baseline -> merge regression, fix it.
- Fails on **both** baselines -> pre-existing (often a brittle test, e.g. a mock
  asserting `NULLIF` appears in a count query that never references the column).
  Does not block the merge.

```bash
git worktree remove /tmp/base --force && git worktree prune
```

---

## Step 5 — Commit & Push

```bash
git add -A
git commit --no-edit     # reuses the auto-generated "Merge branch ..." message
git push origin <new-branch>
```

> Use `--no-edit` to accept the standard merge commit message. Push the branch
> so others (and CI) can build on it.

---

## Complete Command Cheat Sheet

```bash
# Step 1
git fetch upstream
git checkout litellm_internal_staging
git merge upstream/litellm_internal_staging --ff-only   # + git clean -fd if blocked

# Step 2
git checkout v1.96.2
git checkout -b jya0-v1.96.2

# Step 3
git merge jya0-v1.95.0

# Step 4 — resolve, then verify
git ls-files -u | wc -l        # 0 = resolved
git diff --check               # no markers/whitespace
# (dependency + dropped-argument audit + test sweep, see above)

# Step 5
git add -A
git commit --no-edit
git push origin jya0-v1.96.2
```

---

## Lessons Learned

- **Generated files are the sneakiest FF blocker.** `litellm/proxy/_experimental/out/`
  is gitignored on the custom branch but tracked upstream; until `git clean -fd`
  it silently blocks the fast-forward.
- **Taking a whole custom file is a trap if the other side refactored callers.**
  Always cross-check referencing files before picking the entire file.
- **Line-by-line merges drop arguments silently.** Re-diff the resolved call
  against both parents to catch dropped keywords (the `value=team_table` miss).
- **Push even if pre-existing tests fail.** Document them as pre-existing with
  the baseline proof; they aren't merge blockers.