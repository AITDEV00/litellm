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
