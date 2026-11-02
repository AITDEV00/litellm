# Smoke-test suite conventions

Live, post-merge behavior checks against a **deployed** gateway (dev or prod).
This is not CI: it exists so you can run `make smoke-fast` after a merge and
confirm the gateway's documented behaviors survived, before you trust the tree.

**This suite is a mandatory step of the upstream merge.**
[`docs/techniques/upstream_merge_technique.md`](../../docs/techniques/upstream_merge_technique.md)
Step 7 requires updating this suite for anything the merge changed, deploying
the merged image to **dev**, and running `make smoke` against it. A merge is not
done until that passes.

Run from `oicm-litellm-layer/`:

```bash
make smoke-fast         # Tier A: contract only, no inference, ~1-2 min
make smoke-models       # Tier A + B: adds real model calls (costs tokens)
make smoke-leaks        # Tier C read-only: task census + cache boundedness
make smoke-leaks-mutate # Tier C mutating: writes routing_strategy_args, restores it
make smoke              # all read-only tiers (excludes every mutating test)
```

Point at a target with env vars. Defaults are the dev gateway port-forward:

```bash
# dev (default): BASE defaults to http://localhost:14001
kubectl -n adeo-litellm port-forward deploy/litellm-proxy-dev 14001:4000 &
make smoke

# prod: read-only targets only
make smoke BASE=https://litellm.ecouncil.ae
```

## Auth

Two keys are resolved by `conftest.py`:

- `MASTER_KEY` — defaults to the value in
  `deploy/base/gateway/secrets/litellm-master-key.yaml` via
  `scripts/get_master_key.py`. Used for admin CRUD and the admin-only probes.
- `KEY` — a scoped `key_type=llm_api` virtual key. **Required for Tier B and
  for the scoped-key auth tests** (403 on denied model, 429 on rate limit).
  A master key bypasses `allowed_routes`, so it cannot prove a customer key
  works. Export it: `KEY=sk-... make smoke-models`.

## The rules

- **Hard failures only.** No test skips. If the gateway or a key is missing,
  the suite fails loudly, never passes silently. A missing `KEY` fails the
  Tier-B/auth tests, not skips them.
- **Test behavior, not structure.** Assert HTTP status + response-body fields a
  customer sees. Never assert on internal config, file layout, or which module
  served the request.
- **Only the invariant our code guarantees.** Do not pin facts we don't own: a
  provider's exact output text, a latency number, a price. Assert the contract
  (status, field presence/type, error envelope, set membership).
- **A connection failure is never a pass.** `HttpError(status=0)` means the
  gateway was unreachable. Assert `isinstance(result, Success)` first, then the
  status guard; `status < 500` alone lets status 0 slip through.
- **Clean up with `resources`.** Any test that creates state on the stateful
  gateway (a key, a voice profile) registers a teardown with `resources.defer`.
  Teardown is LIFO and best-effort, and runs even when the test fails.
- **Markers select the tier.** `contract` = Tier A, `model_contract` = Tier B,
  `soak` = Tier C, `mutating` = writes live gateway state. The make targets
  select by marker; an unmarked test never runs under any target.

## Mutating tests

Anything marked `mutating` changes live gateway state and is excluded from
`smoke`, `smoke-fast`, `smoke-models`, and `smoke-leaks`. Only
`smoke-leaks-mutate` runs them.

- It must **restore** what it changed and re-assert the restored value in a
  `finally` block. A failed restore is a test failure.
- Use a **DB-owned** setting, not a config-file-owned one:
  `POST /config/update` returns 400 for config-owned keys (see
  `reject_config_owned_writes`) and upserts DB-owned ones permanently.
- **Never point it at prod.** It persists through the DB, so a crash mid-run
  leaves prod altered.

## Adding a test

If you add a documented gateway behavior, add its contract here in the same
file as its feature area (don't create a new file per endpoint). If a merge
drops the behavior, the test fails. If a test needs a new environment secret,
read it from `os.environ` in `conftest.py` and fail (don't skip) when absent.

**Do this in the same PR as the feature.** A documented behavior with no smoke
contract is a behavior the next merge can drop silently. When a behavior is
retired, delete its test and the doc that documents it together.
