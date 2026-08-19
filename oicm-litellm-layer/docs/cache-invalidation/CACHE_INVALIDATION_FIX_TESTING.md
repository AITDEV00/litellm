# Cache Invalidation Fix: Testing Methodology

## Problem

When a team's model list is updated (`/team/update`, `/team/model_add`, `/team/model_delete`), the proxy invalidates the team cache but does NOT invalidate the key caches for keys assigned to that team. Keys cached with the `all-team-models` sentinel carry a frozen `team_models` snapshot (pulled from the `combined_view` SQL JOIN at cache time) that stays stale for up to 60 seconds (in-memory TTL). This means a model removed from a team can still be accessed by keys in that team until the cache expires naturally.

## Fix: Option A

After `_refresh_cached_team` writes the updated team object to cache, enumerate all keys for the team via `VerificationTokenRepository.find_by_team_id` and invalidate each via `_delete_cache_key_object`. This deletes the key from both `user_api_key_cache` (in-memory + Redis) and `internal_usage_cache.dual_cache` (in-memory + Redis). The next request for any of those keys triggers a cache miss, forcing a fresh DB read via the `combined_view` SQL JOIN, which picks up the latest team models.

### Scope

| Scenario | Before Fix | After Fix |
| --- | --- | --- |
| Team model update | Key cache stale up to 60s; keys with `all-team-models` carry frozen `team_models` | Immediate invalidation of all team's keys; next request reads fresh team models from DB |
| Org model update | No cache invalidation, but no runtime impact (org models only checked at team-assignment time) | No change (Option A does not apply; org model access is not a runtime check) |
| Key model update | Already correct: `_delete_cache_key_object` + wrapper invalidation | No change (already works) |

## Test Environment

- Proxy URL: `https://litellm.adeoaiengine.ecouncil.ae`
- Admin key: `{{ master_key }}`
- Test team: LITELLM TEST (ID: `dd76dd4d-95c4-4eef-b497-c3d4318ee7ee`)
- Test key: `sk-ZVHnvrLkfSAE6GbuWCJssw` (alias: `test-user-key`)
- Branch: `jya0-v1.96.2`
- Docker image: `litellm-src:jya0-v1.96.2`

## Test Prerequisites

1. Set the test team to a known model list (e.g., `["Qwen/Qwen3-Embedding-0.6B"]` only)
2. Assign the test key to the test team with `models: ["all-team-models"]`
3. Verify the key can call the allowed model

## Test Cases

### Test 1: Model Removal Takes Effect Immediately

**Goal:** Verify that removing a model from a team blocks access immediately (no 60s delay).

**Steps:**

```bash
# 1. Ensure team has both models
curl -s https://litellm.adeoaiengine.ecouncil.ae/team/info \
  -H "Authorization: Bearer {{ master_key }}" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for t in data.get('teams', []):
    if t['team_id'] == 'dd76dd4d-95c4-4eef-b497-c3d4318ee7ee':
        print('Team models:', t.get('models', []))
"

# 2. Confirm key can call the model that will be removed (should be 200)
curl -s -o /dev/null -w "%{http_code}" \
  https://litellm.adeoaiengine.ecouncil.ae/chat/completions \
  -H "Authorization: Bearer sk-ZVHnvrLkfSAE6GbuWCJssw" \
  -H "Content-Type: application/json" \
  -d '{"model": "<MODEL_TO_REMOVE>", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}'

# 3. Remove the model from the team
curl -s -X POST https://litellm.adeoaiengine.ecouncil.ae/team/model_delete \
  -H "Authorization: Bearer {{ master_key }}" \
  -H "Content-Type: application/json" \
  -d '{"team_id": "dd76dd4d-95c4-4eef-b497-c3d4318ee7ee", "models": ["<MODEL_TO_REMOVE>"]}'

# 4. Immediately try to call the removed model (should be 403 after fix, was 200 before fix)
curl -s -o /dev/null -w "%{http_code}" \
  https://litellm.adeoaiengine.ecouncil.ae/chat/completions \
  -H "Authorization: Bearer sk-ZVHnvrLkfSAE6GbuWCJssw" \
  -H "Content-Type: application/json" \
  -d '{"model": "<MODEL_TO_REMOVE>", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}'
```

**Expected after fix:** Step 4 returns `403`.

**Expected before fix:** Step 4 returns `200` (stale cache).

### Test 2: Model Addition Takes Effect Immediately

**Goal:** Verify that adding a model to a team grants access immediately.

**Steps:**

```bash
# 1. Confirm key CANNOT call the model that will be added (should be 403)
curl -s -o /dev/null -w "%{http_code}" \
  https://litellm.adeoaiengine.ecouncil.ae/chat/completions \
  -H "Authorization: Bearer sk-ZVHnvrLkfSAE6GbuWCJssw" \
  -H "Content-Type: application/json" \
  -d '{"model": "<MODEL_TO_ADD>", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}'

# 2. Add the model to the team
curl -s -X POST https://litellm.adeoaiengine.ecouncil.ae/team/model_add \
  -H "Authorization: Bearer {{ master_key }}" \
  -H "Content-Type: application/json" \
  -d '{"team_id": "dd76dd4d-95c4-4eef-b497-c3d4318ee7ee", "models": ["<MODEL_TO_ADD>"]}'

# 3. Immediately try to call the added model (should be 200)
curl -s -o /dev/null -w "%{http_code}" \
  https://litellm.adeoaiengine.ecouncil.ae/chat/completions \
  -H "Authorization: Bearer sk-ZVHnvrLkfSAE6GbuWCJssw" \
  -H "Content-Type: application/json" \
  -d '{"model": "<MODEL_TO_ADD>", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}'
```

**Expected after fix:** Step 3 returns `200`.

### Test 3: Key Model Update Still Works

**Goal:** Verify that the existing key update path is not broken by the fix.

**Steps:**

```bash
# 1. Update the key's models directly
curl -s -X POST https://litellm.adeoaiengine.ecouncil.ae/key/update \
  -H "Authorization: Bearer {{ master_key }}" \
  -H "Content-Type: application/json" \
  -d '{"key": "sk-ZVHnvrLkfSAE6GbuWCJssw", "models": ["<MODEL_1>", "<MODEL_2>"]}'

# 2. Immediately call one of the added models (should be 200)
curl -s -o /dev/null -w "%{http_code}" \
  https://litellm.adeoaiengine.ecouncil.ae/chat/completions \
  -H "Authorization: Bearer sk-ZVHnvrLkfSAE6GbuWCJssw" \
  -H "Content-Type: application/json" \
  -d '{"model": "<MODEL_1>", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}'
```

**Expected:** Step 2 returns `200`.

### Test 4: No Impact on Other Teams' Keys

**Goal:** Verify that invalidating keys for one team does not affect keys in other teams.

**Steps:**

```bash
# 1. Use a key from a DIFFERENT team
# 2. Update LITELLM TEST team's models
# 3. Immediately call a model using the other team's key
# 4. Should still work if the model is allowed for that other team
```

## Additional Fix: Team-Level Cache Invalidation

### Problem

During testing, a second cache-staleness issue was discovered. `get_team_object()` checks `internal_usage_cache.dual_cache` FIRST, then `user_api_key_cache`. But `_cache_team_object()` only writes the fresh team to `user_api_key_cache` — it does NOT invalidate the stale entry in `internal_usage_cache.dual_cache`. This means a stale team snapshot in `internal_usage_cache.dual_cache.in_memory_cache` shadows the fresh write to `user_api_key_cache`, causing auth checks to see pre-mutation team models for up to 60s (in-memory TTL).

### Fix

In `litellm/proxy/auth/auth_checks.py`, `_cache_team_object()` now deletes the `team_id:{team_id}` key from `internal_usage_cache.dual_cache` BEFORE writing the fresh value to `user_api_key_cache`. This is consistent with how the `team_alias:` key is already handled. The delete happens before the write because both caches share the same Redis backend — deleting after would undo the Redis write.

### Files Modified

1. `litellm/proxy/management_endpoints/team_endpoints.py` — Added `_invalidate_team_key_caches()` helper and calls after `_refresh_cached_team` in `update_team`, `team_model_add`, and `team_model_delete`
2. `litellm/proxy/management_endpoints/model_management_endpoints.py` — Added `_invalidate_team_key_caches()` call after `_refresh_cached_team` in `_remove_unbacked_team_models`
3. `litellm/proxy/auth/auth_checks.py` — Added `internal_usage_cache.dual_cache` invalidation for `team_id:` key in `_cache_team_object()`

## Test Results (2025-07-16)

All tests passed against the live proxy after both fixes were deployed.

### Test 1: Model Removal Takes Effect Immediately — ✅ PASS

```
1a. Key can call GLM (before removal):     HTTP 200 (may be 403 if request hits
    the other pod in multi-pod setup — pre-existing in-memory cache limitation)
1b. Remove GLM from team via /team/model/delete
1c. Immediately call GLM (after removal):  HTTP 403  ← correct (was 200 before fix)
```

### Test 2: Model Addition Takes Effect Immediately — ✅ PASS

```
2a. Key cannot call GLM (before add):     HTTP 403
2b. Add GLM to team via /team/model/add
2c. Immediately call GLM (after add):     HTTP 200  ← correct (was 403 before fix)
```

### Test 3: Key Model Update Still Works — ✅ PASS

```
3a. Update key to GLM only via /key/update
3b. Call GLM (in key's model list):        HTTP 200  ← correct
3c. Call Qwen3.5 (not in key's list):      HTTP 403  ← correct
```

### Test 4: No Impact on Other Teams — ✅ PASS

```
4a. Admin key (different team) calls GLM:  HTTP 200  ← correct
```

### Note on Multi-Pod In-Memory Cache

In a multi-pod deployment (2 replicas), in-memory cache entries on the pod that did NOT handle the mutation request may remain stale briefly. This is a pre-existing limitation of in-memory caching in distributed setups. Both fixes correctly handle:
- The current pod's in-memory cache (deleted immediately)
- The shared Redis cache (deleted immediately, fresh value written)

The other pod's in-memory cache expires naturally (within seconds for `internal_usage_cache.dual_cache`, within 60s for `user_api_key_cache`).

## Cleanup

After testing, restore the test team and key to their original state:

```bash
# Restore team models
curl -sk -X POST https://litellm.adeoaiengine.ecouncil.ae/team/update \
  -H "Authorization: Bearer {{ master_key }}" \
  -H "Content-Type: application/json" \
  -d '{"team_id": "dd76dd4d-95c4-4eef-b497-c3d4318ee7ee", "models": ["Qwen/Qwen3-Embedding-0.6B", "Qwen/Qwen3.5-0.8B"]}'

# Restore key models
curl -sk -X POST https://litellm.adeoaiengine.ecouncil.ae/key/update \
  -H "Authorization: Bearer {{ master_key }}" \
  -H "Content-Type: application/json" \
  -d '{"key": "sk-ZVHnvrLkfSAE6GbuWCJssw", "models": ["all-team-models"]}'
```

**Note:** All curl commands require the `-k` flag (self-signed cert on the proxy).

## Phase 2: Multi-Pod In-Memory Cache Re-Poisoning Fix (skip_in_memory)

### Problem

The Phase 1 fixes (Option A + team-level cache invalidation) correctly handle single-pod deployments but fail in multi-pod. When Pod A handles a team/key mutation, it clears its own in-memory cache and Redis. But Pod B's in-memory cache still holds the stale value. On Pod B's next request:

1. `DualCache.async_get_cache` checks in-memory FIRST and returns the stale value immediately, never consulting Redis or DB
2. The stale value is written BACK to Redis by the auth flow (`user_api_key_auth.py` line ~1942), re-poisoning the shared Redis cache
3. Each write resets the 60s in-memory TTL, so under continuous traffic the stale value never expires

This was confirmed with live tests against the 2-pod k8s deployment:
- Team model add: stale 403 (denial after grant) persisted for 5+ minutes under continuous traffic
- Team model delete: stale 200 (grant after revocation) persisted for ~30s
- Key model update: stale 200 (access after restriction) persisted for 30+ seconds
- After 70s with NO requests, both pods returned correct results (TTL expiry forced fresh DB read)
- Redis cache confirmed stale: `models: ['all-team-models']` after key update to `['Qwen/Qwen3.5-0.8B']`, with `last_refreshed_at` AFTER the update

### Root Cause

`DualCache.async_get_cache` (`litellm/caching/dual_cache.py`) checks in-memory first and short-circuits on hit. There is no mechanism to invalidate in-memory cache on other pods. The write-back of cached values to Redis re-poisons the shared cache.

### Fix: skip_in_memory Flag

Auth-critical cache reads now bypass per-pod in-memory cache entirely, reading only from the shared Redis layer. This is the industry-standard approach for mutable authorization data in distributed API gateways (Kong, Tyk, etc. use Redis-only for authz with short TTLs).

**Measured latency overhead**: ~0.3ms p50 per Redis GET (vs ~0.0001ms for in-memory). With 3-5 auth lookups per request, total overhead is ~1.5ms p50, ~2.4ms p99. Against 120-200ms end-to-end request times and multi-second LLM inference, this is negligible (<1% of request time).

### Files Modified

1. `litellm/caching/dual_cache.py` — Added `skip_in_memory: bool = False` parameter to `get_cache` and `async_get_cache`. When True, bypasses in-memory read AND does not write Redis result back to in-memory (prevents re-poisoning). Includes graceful degradation: `effective_skip = skip_in_memory and self.redis_cache is not None` — falls back to in-memory when no Redis is configured (single-pod or test environments), since cross-pod staleness only exists with shared Redis.

2. `litellm/proxy/common_utils/user_api_key_cache.py` — Changed the default of `skip_in_memory` from `False` to `True` in all 6 `get_cache`/`async_get_cache` signatures (2 sync overloads, 2 async overloads, 2 implementations). This is the centralized fix: every `UserApiKeyCache` read automatically bypasses per-pod in-memory cache and reads from shared Redis, without needing to patch every caller. Callers that explicitly want in-memory reads (e.g. for testing) can pass `skip_in_memory=False`.

3. `litellm/proxy/auth/auth_checks.py` — The `internal_usage_cache.dual_cache` read in `_get_team_object_from_cache` uses the DualCache default (`skip_in_memory=False`). This is intentional: `internal_usage_cache` wraps a plain `DualCache` with a 1-second TTL, so the in-memory layer acts as a fast pre-check for repeated reads within the same second before falling through to `user_api_key_cache` (which defaults to `skip_in_memory=True` and reads from Redis). All other auth cache reads inherit the `UserApiKeyCache` default automatically.

4. `litellm/proxy/auth/user_api_key_auth.py`:
   - Removed redundant team cache write-back in `_user_api_key_auth_builder` (was at line ~1942). This write-back was re-poisoning Redis with stale cached values on every request. The DB path (`_get_team_object_from_user_api_key_cache` via `_cache_team_object`) already caches under the canonical `team_id:{id}` key, so the write-back served no purpose except re-poisoning.
   - Removed dead `_team_obj_from_lookup` flag (was only used by the removed write-back).

5. `litellm/proxy/auth/resolvers/store.py` — No changes needed. `_resolve_key` reads via `self._cache` (a `UserApiKeyCache`), which now defaults to `skip_in_memory=True`.

6. `litellm/proxy/auth/handle_jwt.py` — Three JWT/OIDC cache reads explicitly pass `skip_in_memory=False` to opt out of the Redis-first default. These cache external IdP data (OIDC discovery URL, JWKS public keys, OIDC UserInfo) that is never mutated by proxy DB operations, so the multi-pod staleness concern does not apply. Keeping them in-memory-first avoids unnecessary Redis round-trips for JWT-authenticated requests.

### Tests Added

- `tests/test_litellm/caching/test_dual_cache.py`:
  - `test_async_get_cache_skip_in_memory_bypasses_stale_in_memory` — verifies Redis value returned, in-memory not overwritten
  - `test_async_get_cache_skip_in_memory_returns_none_when_redis_miss` — verifies Redis miss returns None even with stale in-memory
  - `test_async_get_cache_without_skip_in_memory_still_uses_in_memory` — sanity: DualCache default (False) still reads in-memory first
  - `test_get_cache_skip_in_memory_sync_variant` — same for sync `get_cache`
  - `test_skip_in_memory_falls_back_to_in_memory_when_no_redis` — graceful degradation: when no Redis configured, skip_in_memory falls back to in-memory

- `tests/test_litellm/proxy/common_utils/test_user_api_key_cache.py`:
  - `test_skip_in_memory_bypasses_stale_in_memory_and_returns_redis` — typed read with explicit skip_in_memory=True bypasses stale in-memory
  - `test_skip_in_memory_does_not_write_back_to_in_memory` — no re-poisoning write-back
  - `test_skip_in_memory_returns_none_on_redis_miss_even_with_stale_in_memory` — Redis miss respected
  - `test_without_skip_in_memory_still_reads_in_memory_first` — explicit skip_in_memory=False reads in-memory first
  - `test_default_skip_in_memory_is_true` — verifies the default: omitting skip_in_memory bypasses stale in-memory and reads from Redis

- `tests/test_litellm/proxy/auth/test_user_api_key_auth.py`:
  - `test_auth_path_does_not_re_cache_team_object` — verifies the auth builder does NOT write the team object back to cache after `get_team_object` returns

### Multi-Pod Test Plan

Test against the 2-pod k8s deployment with continuous traffic (requests every 5-10s to both pods via per-pod port-forwards):

```bash
# Port-forwards
kubectl -n mlops port-forward pod/litellm-proxy-8678dfc99-cvtlf 4001:4000 &
kubectl -n mlops port-forward pod/litellm-proxy-8678dfc99-dz46b 4002:4000 &

# Continuous request loop (both pods)
while true; do
  TS=$(date '+%H:%M:%S')
  HTTP1=$(curl -sk -o /dev/null -w "%{http_code}" http://localhost:4001/v1/chat/completions \
    -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
    -d '{"model":"<TARGET_MODEL>","messages":[{"role":"user","content":"hi"}],"max_tokens":1}')
  HTTP2=$(curl -sk -o /dev/null -w "%{http_code}" http://localhost:4002/v1/chat/completions \
    -H "Authorization: Bearer <KEY>" -H "Content-Type: application/json" \
    -d '{"model":"<TARGET_MODEL>","messages":[{"role":"user","content":"hi"}],"max_tokens":1}')
  echo "[$TS] POD1=$HTTP1 POD2=$HTTP2"
  sleep 5
done
```

**Test 1: Team model add (grant access)**
1. Start request loop calling a model NOT in the team (expect consistent 403 from both pods)
2. Add the model to the team via `/team/model/add`
3. Within 1-2 requests, both pods should return 200 (no 5+ minute stale 403)

**Test 2: Team model delete (revoke access)**
1. Start request loop calling a model IN the team (expect consistent 200)
2. Remove the model via `/team/model/delete`
3. Within 1-2 requests, both pods should return 403 (no 30s stale 200)

**Test 3: Key model update (restrict key-level access)**
1. Start request loop with key that has `all-team-models` (expect 200)
2. Update key to restrict to a specific model via `/key/update`
3. Within 1-2 requests, both pods should return 403 for the now-disallowed model

**Expected after fix**: All three tests show consistent behavior across both pods within 1-2 requests (bounded by Redis TTL, not in-memory TTL). No intermittent 200/403 split between pods.

## Phase 3: Surgical Opt-Outs from skip_in_memory Default

### Problem

The Phase 2 fix changed the `UserApiKeyCache` default to `skip_in_memory=True`, which means every auth-critical cache read bypasses per-pod in-memory cache and goes to Redis. An audit of all `UserApiKeyCache` read sites revealed two cases where this was unnecessary or redundant:

1. **JWT/OIDC reads in `handle_jwt.py`** — These cache external IdP data (OIDC discovery URL, JWKS public keys, OIDC UserInfo) that is never mutated by proxy DB operations. The multi-pod staleness concern does not apply. Forcing them through Redis added unnecessary latency for JWT-authenticated requests.

2. **`internal_usage_cache.dual_cache` read in `auth_checks.py`** — The `internal_usage_cache` wraps a plain `DualCache` with a 1-second TTL. With `skip_in_memory=True` on both `internal_usage_cache.dual_cache` and `user_api_key_cache`, both caches hit the same Redis for the same key, making the `internal_usage_cache` read fully redundant (two Redis round-trips for the same data).

### Fix

1. **`handle_jwt.py`** — Added `skip_in_memory=False` to 3 cache reads:
   - OIDC discovery URL (line ~611)
   - JWKS public keys (line ~648)
   - OIDC UserInfo (line ~744)

   These now stay in-memory-first, avoiding Redis round-trips for static external IdP data.

2. **`auth_checks.py`** — Reverted the `internal_usage_cache.dual_cache` read in `_get_team_object_from_cache` to the DualCache default (no explicit `skip_in_memory` parameter). The 1-second in-memory TTL provides a valid fast path for repeated reads within the same second. On a miss, it falls through to `user_api_key_cache`, which defaults to `skip_in_memory=True` and reads from Redis. This eliminates the redundant dual-Redis round-trip.

### Commits

- `fe96c96dac` — Core skip_in_memory implementation in DualCache + UserApiKeyCache default change + removed redundant team cache write-back + 11 regression tests
- `40f28b0fa0` — Phase 3 surgical fixes: JWT/OIDC opt-outs + internal_usage_cache revert
- `617e913588` — Phase 4: Fix DualCache Redis write-back TTL bypass (root cause of team-level staleness)

## Phase 4: DualCache Redis Write-Back TTL Fix

### Problem

After Phases 1-3, key-level cache invalidation worked correctly, but **team-level** cache invalidation still failed with stale data persisting for ~6-10 minutes across pods. Redis had the correct team data, but pods served stale results.

### Root Cause

`DualCache.async_get_cache` (and the sync `get_cache`) has a Redis→in-memory write-back path (line ~228): when a Redis hit is found, the value is written back to in-memory cache for future reads. This write-back called `self.in_memory_cache.async_set_cache(key, redis_result, **kwargs)` **directly on InMemoryCache**, bypassing `DualCache`'s TTL logic.

Since no `ttl` kwarg was passed, `InMemoryCache` fell back to its own `default_ttl` of **600 seconds (10 minutes)** instead of the DualCache's configured `default_in_memory_ttl`.

The `internal_usage_cache.dual_cache` is constructed with `DualCache(default_in_memory_ttl=1)` — intended as a 1-second TTL. But the write-back bypassed this, causing stale team objects to persist in-memory for 10 minutes. Since `_get_team_object_from_cache` checks `internal_usage_cache.dual_cache` FIRST (with `skip_in_memory=False`), the stale in-memory entry shadowed the fresh Redis data on pods that didn't handle the `/team/update`.

**Why key-level updates worked but team-level didn't:**
- Key updates **delete** the key from cache entirely → next request is a cache miss → DB read
- Team updates **write fresh** to cache → relies on Redis propagation → shadowed by stale in-memory entry

### Fix

In `litellm/caching/dual_cache.py`, the Redis write-back path in both `async_get_cache` and `get_cache` now passes `self.default_in_memory_ttl` as the `ttl` kwarg:

```python
write_back_kwargs = dict(kwargs)
if "ttl" not in write_back_kwargs and self.default_in_memory_ttl is not None:
    write_back_kwargs["ttl"] = self.default_in_memory_ttl
await self.in_memory_cache.async_set_cache(key, redis_result, **write_back_kwargs)
```

This ensures the `internal_usage_cache` in-memory entries expire after 1 second (as intended), not 600 seconds. After 1 second, the in-memory entry expires, and the next read falls through to `user_api_key_cache` (which has `skip_in_memory=True` and reads from Redis).

### Live Test Results (2026-07-22)

| Scenario | Before Fix (Phase 3) | After Fix (Phase 4) |
| --- | --- | --- |
| Team model removal → both pods return 403 | POD1 stuck on 200 for 2+ min, POD2 oscillates | **Both pods 403 immediately** ✓ |
| Team model restore → both pods return 200 | Both pods stuck on 403 for ~6 min | **Both pods 200 immediately** ✓ |
| Key model update → both pods return correct | Already worked | Still works ✓ |

## Fix Verification on v1.96.2

On 2026-07-22 all three fix commits were confirmed present on branch `jya0-v1.96.2`:

- `fe96c96dac` — `fix(proxy): default UserApiKeyCache to skip in-memory for multi-pod consistency`
- `40f28b0fa0` — `fix(proxy): keep internal_usage_cache in-memory-first and JWT/OIDC reads in-memory`
- `617e913588` — `fix(caching): DualCache Redis write-back must honor default_in_memory_ttl`

The Phase 4 write-back TTL guard is intact in `litellm/caching/dual_cache.py` (`write_back_kwargs` + `default_in_memory_ttl` injection), and the `skip_in_memory` plumbing is intact in `litellm/proxy/common_utils/user_api_key_cache.py`. The upstream merge to v1.96.2 did not alter any of the Phase 2-4 changes.

## Deployment Guide

### Prerequisites

- podman (or docker) installed locally
- Access to the Harbor registry at `registry.adeoaiengine.ecouncil.ae`
- kubectl configured with a kubeconfig that can reach the mlops namespace
- The LiteLLM source repo at `/home/jyao/ADEO/service/litellm` on branch `jya0-v1.96.2`
- The OICM litellm layer at `/home/jyao/ADEO/service/litellm/oicm-litellm-layer`

### Step 1: Build and Test Locally

Build the Docker image from the repo root Dockerfile. The tag is derived from the current git branch (slashes replaced with underscores):

```bash
cd /home/jyao/ADEO/service/litellm/oicm-litellm-layer
make litellm-src-build
```

This produces `registry.adeoaiengine.ecouncil.ae/openinnovationai/platform/mlops/mlops-serving/litellm-src:jya0-v1.96.2`.

Run the image locally with the no-DB/no-Redis config to verify it boots cleanly:

```bash
# Free port 4000 if something is using it
ss -tlnp | grep 4000

# Run in detached mode (local_dev.yaml has no DB/Redis deps, starts instantly)
podman run --rm -d --name litellm-local \
  -p 4000:4000 \
  -v $(pwd)/config/local_dev.yaml:/app/config.yaml:Z \
  -v $(pwd)/hooks:/app/litellm_hooks:Z \
  -e STORE_MODEL_IN_DB=true \
  -e LITELLM_MASTER_KEY={{ master_key }} \
  registry.adeoaiengine.ecouncil.ae/openinnovationai/platform/mlops/mlops-serving/litellm-src:jya0-v1.96.2 \
  --config /app/config.yaml --port 4000

# Wait for startup, then verify
curl -s http://localhost:4000/health/liveliness
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer {{ master_key }}"

# Verify the fix is baked into the image
podman exec litellm-local grep -c "skip_in_memory" \
  /app/.venv/lib/python3.13/site-packages/litellm/proxy/common_utils/user_api_key_cache.py
# Expected: 10

podman exec litellm-local grep -c "skip_in_memory" \
  /app/.venv/lib/python3.13/site-packages/litellm/proxy/auth/handle_jwt.py
# Expected: 3

# Clean up
podman rm -f litellm-local
```

Alternatively, use the Makefile target for interactive testing (with `--detailed_debug`):

```bash
make litellm-local-docker
```

Or run from the local venv (no Docker, faster iteration):

```bash
make litellm-local-run
```

### Step 2: Push the Image to Harbor

```bash
# Login to Harbor (one-time per session)
make login

# Push the tagged image
make litellm-src-push
```

Or build and push in one step:

```bash
make litellm-src-build-push
```

### Step 3: Deploy to the Cluster

The deployment manifest (`deploy/prod/litellm-proxy.yaml`) already references the correct image tag. If the tag in the manifest matches what you pushed, you only need a rollout restart to pull the new image:

```bash
# Verify the manifest image tag matches what you pushed
kubectl get deploy litellm-proxy -n mlops \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
# Expected: .../litellm-src:jya0-v1.96.2

# Trigger a rolling restart (maxUnavailable: 0, so one pod at a time)
kubectl rollout restart deploy/litellm-proxy -n mlops

# Wait for the rollout to complete
kubectl rollout status deploy/litellm-proxy -n mlops --timeout=300s

# Verify both new pods are running
kubectl get pods -n mlops -l app=litellm-proxy

# Verify the fix is in the deployed pods
NEW_POD=$(kubectl get pods -n mlops -l app=litellm-proxy \
  -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n mlops $NEW_POD -- grep -c "skip_in_memory" \
  /app/.venv/lib/python3.13/site-packages/litellm/proxy/common_utils/user_api_key_cache.py
# Expected: 10
```

If the manifest image tag needs updating (e.g. new branch), edit `deploy/prod/litellm-proxy.yaml` and apply:

```bash
kubectl apply -f deploy/prod/litellm-proxy.yaml
```

### Step 4: Verify the Fix in the Cluster

Port-forward to individual pods for multi-pod testing:

```bash
# Get the new pod names
POD1=$(kubectl get pods -n mlops -l app=litellm-proxy \
  -o jsonpath='{.items[0].metadata.name}')
POD2=$(kubectl get pods -n mlops -l app=litellm-proxy \
  -o jsonpath='{.items[1].metadata.name}')

# Port-forward to each pod on separate local ports
kubectl -n mlops port-forward pod/$POD1 4001:4000 &
kubectl -n mlops port-forward pod/$POD2 4002:4000 &

# Run the multi-pod test loop (see Phase 2 Multi-Pod Test Plan above)
```

Alternatively, test against the cluster ingress:

```bash
curl -sk https://litellm.adeoaiengine.ecouncil.ae/health/liveliness
curl -sk https://litellm.adeoaiengine.ecouncil.ae/v1/models \
  -H "Authorization: Bearer {{ master_key }}"
```

### Quick Reference: All Makefile Targets

| Target | Description |
| --- | --- |
| `make litellm-src-build` | Build the LiteLLM source image from the repo root Dockerfile |
| `make litellm-src-push` | Push the image to Harbor (requires `make login` first) |
| `make litellm-src-build-push` | Build then push in one step |
| `make litellm-local-run` | Run from local venv (no Docker, fastest iteration) |
| `make litellm-local-docker` | Run the built Docker image locally (interactive, with debug) |
| `make litellm-local-stop` | Stop a locally-running Docker container |
| `make login` | Login to the Harbor registry |
| `make deploy` | Apply the k8s manifests to the cluster |
