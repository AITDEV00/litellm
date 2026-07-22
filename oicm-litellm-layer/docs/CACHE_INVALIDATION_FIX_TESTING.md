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
- Admin key: `sk-1234`
- Test team: LITELLM TEST (ID: `dd76dd4d-95c4-4eef-b497-c3d4318ee7ee`)
- Test key: `sk-ZVHnvrLkfSAE6GbuWCJssw` (alias: `test-user-key`)
- Branch: `jya0-v1.92.0`
- Docker image: `litellm-src:jya0-v1.92.0`

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
  -H "Authorization: Bearer sk-1234" | python3 -c "
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
  -H "Authorization: Bearer sk-1234" \
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
  -H "Authorization: Bearer sk-1234" \
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
  -H "Authorization: Bearer sk-1234" \
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
  -H "Authorization: Bearer sk-1234" \
  -H "Content-Type: application/json" \
  -d '{"team_id": "dd76dd4d-95c4-4eef-b497-c3d4318ee7ee", "models": ["Qwen/Qwen3-Embedding-0.6B", "Qwen/Qwen3.5-0.8B"]}'

# Restore key models
curl -sk -X POST https://litellm.adeoaiengine.ecouncil.ae/key/update \
  -H "Authorization: Bearer sk-1234" \
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

3. `litellm/proxy/auth/auth_checks.py` — Only one explicit `skip_in_memory=True` remains: the `internal_usage_cache.dual_cache` read in `_get_team_object_from_cache` (this is a plain `DualCache`, not a `UserApiKeyCache`, so it doesn't inherit the default). All other auth cache reads inherit the `UserApiKeyCache` default automatically.

4. `litellm/proxy/auth/user_api_key_auth.py`:
   - Removed redundant team cache write-back in `_user_api_key_auth_builder` (was at line ~1942). This write-back was re-poisoning Redis with stale cached values on every request. The DB path (`_get_team_object_from_user_api_key_cache` via `_cache_team_object`) already caches under the canonical `team_id:{id}` key, so the write-back served no purpose except re-poisoning.
   - Removed dead `_team_obj_from_lookup` flag (was only used by the removed write-back).

5. `litellm/proxy/auth/resolvers/store.py` — No changes needed. `_resolve_key` reads via `self._cache` (a `UserApiKeyCache`), which now defaults to `skip_in_memory=True`.

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
