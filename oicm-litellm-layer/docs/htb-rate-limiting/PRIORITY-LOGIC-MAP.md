# Priority Rate Limiting — Complete Logic Map

> Built with the [Logic Mapping Technique](../techniques/logic_mapping_technique.md):
> trace every function and data flow end-to-end before changing code.
> Every entry below has a verified `file:line` reference in the current
> codebase (branch `jya0-v1.92.0`). This map is the single source of truth
> for understanding priority enforcement.

---

## Glossary (one-screen reference)

| Term | Meaning |
|---|---|
| **HTB** | Hierarchical Token Bucket. Each priority gets a guaranteed rate and can borrow unused sibling capacity. |
| **`htb_priority`** | The single `ContextVar[Optional[str]]` that carries a request's priority from the proxy layer to the router layer. Declared at `litellm/proxy/hooks/dynamic_rate_limiter_v3.py:37`. (The earlier `htb_approved: ContextVar[bool]` was removed; see changelog in `HTB-README.md`.) |
| **Demand counter** | Sliding-window count of *attempted* requests for a priority, incremented *before* the atomic Lua check. Makes a priority's demand visible to siblings immediately. Replaced the earlier EWMA approach. |
| **Borrow ceiling** | `min(saturation_cap, model_limit) - sum(min(sibling_demand, sibling_guaranteed))`, floored at `priority_limit`. The maximum count a priority may reach when borrowing. |
| **Saturation cap** | `model_limit * saturation_threshold` (default `1.0`, so cap = model_limit). Ceiling on borrow headroom. |
| **Lua script** | `HTB_CHECK_AND_INCREMENT_SCRIPT` at `parallel_request_limiter_v3.py:183`. Atomic read + decide + increment inside Redis. |
| **Hash tag** | `{htb:<model>}` Redis Cluster hash tag so all keys for a model live on one shard, enabling atomic Lua access. |
| **`ModelRateLimitingCheck`** | The flat per-deployment RPM/TPM check. Skipped entirely when `htb_priority.get() is not None`. |

---

## Entry Point and Exit Points

**Entry point:** a client `POST /v1/chat/completions` with an API key whose
metadata (or team metadata) carries a `priority` field.

**Exit points (three, mutually exclusive):**
1. **200 OK from the primary model** — HTB allowed, request served.
2. **200 OK from a fallback model** — primary denied, router cascaded, a fallback's HTB check allowed.
3. **429 Rate Limit Exceeded to the client** — every deployment in the fallback chain denied (or no fallbacks configured).

---

## Top-Level Flow

```
Client request (API key with metadata.priority)
    |
    v
[1] Proxy layer: async_pre_call_hook  (dynamic_rate_limiter_v3.py:382)
    |  - extract priority from user_api_key_dict
    |  - htb_priority.set(priority)
    |
    v
[2] Router: picks deployment (primary)
    |
    v
[3] Router: async_routing_strategy_pre_call_checks  (router.py:7204)
    |  - iterates litellm.callbacks, calls async_pre_call_check on each
    |
    +---> [3a] DynamicRateLimitHandlerV3.async_pre_call_check  (dynamic_rate_limiter_v3.py:395)
    |         |  - reads htb_priority.get()
    |         |  - _run_htb_check -> htb_check_and_increment
    |         |       |
    |         |       v
    |         |     [Lua script in Redis]  (parallel_request_limiter_v3.py:183)
    |         |       - read priority/model counters
    |         |       - read sibling demand counters
    |         |       - compute borrow_ceiling
    |         |       - decide ALLOW / OVER_LIMIT
    |         |       - increment on ALLOW
    |         |
    |         +-- ALLOW  -> return deployment  (router proceeds)
    |         +-- OVER_LIMIT -> _raise_rate_limit_error  (dynamic_rate_limiter_v3.py:347)
    |                          raises litellm.RateLimitError
    |
    +---> [3b] ModelRateLimitingCheck.async_pre_call_check  (model_rate_limit_check.py:163)
              |  - reads htb_priority.get()
              |  - if not None: return deployment  (SKIP flat check)
              |  - else: flat RPM/TPM enforcement
    |
    v
[4] Router sends request to model
    |
    +---> success: 200 OK to client
    +---> RateLimitError caught by router:
              - _set_cooldown_deployments  (router.py:7237)
              - pick next fallback deployment -> back to [3]
              - if no fallbacks left: 429 to client
```

---

## Phase 1 — Trace: Function-Level Call Chain

### 1.1 Proxy layer: extract priority

**`async_pre_call_hook`** — `litellm/proxy/hooks/dynamic_rate_limiter_v3.py:382`

```python
async def async_pre_call_hook(
    self,
    user_api_key_dict: UserAPIKeyAuth,
    cache: DualCache,
    data: dict,
    call_type: CallTypesLiteral,
) -> Optional[Union[Exception, str, dict]]:
    if "model" not in data:
        return None
    priority = self._get_priority_from_user_api_key_dict(user_api_key_dict=user_api_key_dict)
    htb_priority.set(priority)
    return None
```

- **No HTB check here.** This hook is intentionally lightweight; it runs once per request, before the router has picked a deployment.
- **ContextVar set:** `htb_priority.set(priority)` at line 389. `htb_priority` is declared at `dynamic_rate_limiter_v3.py:37`.
- **Dispatch site:** `litellm/proxy/utils.py` only calls `async_pre_call_hook` for hooks that override it.

**Priority extraction** — `_get_priority_from_user_api_key_dict` at `dynamic_rate_limiter_v3.py:110`:

```python
def _get_priority_from_user_api_key_dict(self, user_api_key_dict: UserAPIKeyAuth) -> Optional[str]:
    priority: Optional[str] = None
    if user_api_key_dict.team_metadata is not None:
        priority = user_api_key_dict.team_metadata.get("priority", None)
    if priority is None:
        priority = user_api_key_dict.metadata.get("priority", None)
    return priority
```

- **team_metadata takes precedence** over key-level `metadata`. If neither has `priority`, returns `None`.
- `None` priority is handled later in `_get_priority_allocation` (`dynamic_rate_limiter_v3.py:158`): it maps to the shared `{model}:default_pool` key with weight `PriorityReservationSettings.default_priority` (default `0.25`, `litellm/types/utils.py:3716`).

### 1.2 Router layer: HTB enforcement per deployment

**`async_routing_strategy_pre_call_checks`** — `litellm/router.py:7204`

```python
async def async_routing_strategy_pre_call_checks(self, deployment, parent_otel_span, logging_obj=None):
    for _callback in litellm.callbacks:
        if isinstance(_callback, CustomLogger):
            try:
                await _callback.async_pre_call_check(deployment, parent_otel_span)
            except litellm.RateLimitError as e:
                ...
                _set_cooldown_deployments(
                    litellm_router_instance=self,
                    exception_status=e.status_code,
                    original_exception=e,
                    deployment=deployment["model_info"]["id"],
                    time_to_cooldown=self.cooldown_time,
                )
                raise e
            except Exception as e:
                ...
                raise e
```

- **Iterates every callback** in `litellm.callbacks` that is a `CustomLogger`. Both `_PROXY_DynamicRateLimitHandlerV3.async_pre_call_check` and `ModelRateLimitingCheck.async_pre_call_check` fire on the same deployment.
- **On `RateLimitError`:** sets a cooldown on the deployment and re-raises so the router can fall back.
- **Call sites:** this method is invoked from ~14 locations in `router.py` (e.g. lines 2709, 3626, 3732, 3846, 3921, 4113), always inside the per-deployment semaphore, gated by the routing strategy.

### 1.3 HTB pre-call check

**`async_pre_call_check`** — `dynamic_rate_limiter_v3.py:395`

```python
async def async_pre_call_check(self, deployment: dict, parent_otel_span: Optional[Span]) -> Optional[dict]:
    if litellm.priority_reservation is None:
        return deployment

    priority = htb_priority.get()

    model_group = deployment.get("model_name", "")
    if not model_group:
        return deployment

    model_group_info: Optional[ModelGroupInfo] = self.llm_router.get_model_group_info(model_group=model_group)
    if model_group_info is None:
        return deployment
    if model_group_info.rpm is None and model_group_info.tpm is None:
        return deployment

    try:
        htb_response = await self._run_htb_check(
            model=model_group,
            model_group_info=model_group_info,
            priority=priority,
            parent_otel_span=parent_otel_span,
        )
    except Exception as e:
        verbose_proxy_logger.error(f"[HTB] async_pre_call_check error: {e}, allowing request")
        return deployment

    if htb_response["overall_code"] != "OVER_LIMIT":
        return deployment

    self._raise_rate_limit_error(
        model=model_group,
        model_group_info=model_group_info,
        priority=priority,
        htb_response=htb_response,
    )
```

- **Early returns** (fail-open): when `litellm.priority_reservation` is not configured, when `model_name` is missing, when `model_group_info` is missing, when both `rpm` and `tpm` are `None`, or when an exception is raised inside `_run_htb_check`.
- **`_run_htb_check`** is at `dynamic_rate_limiter_v3.py:310`. **`_raise_rate_limit_error`** is at `dynamic_rate_limiter_v3.py:347`.
- **`pre_call_check`** (sync, `dynamic_rate_limiter_v3.py:432`) is a no-op (`return deployment`) because the router calls both sync and async variants; HTB only runs on the async path.

### 1.4 Building descriptors and siblings

**`_run_htb_check`** — `dynamic_rate_limiter_v3.py:310`

```python
async def _run_htb_check(self, model, model_group_info, priority, parent_otel_span) -> RateLimitResponse:
    priority_descriptors = self._create_priority_based_descriptors(model=model, priority=priority)
    if not priority_descriptors:
        return RateLimitResponse(overall_code="OK", statuses=[])

    model_descriptor = self._create_model_tracking_descriptor(
        model=model, model_group_info=model_group_info, high_limit_multiplier=1,
    )
    sibling_priorities = self._get_sibling_priorities(
        model=model, model_group_info=model_group_info, current_priority=priority,
    )
    htb_response = await self.v3_limiter.htb_check_and_increment(
        priority_descriptor=priority_descriptors[0],
        model_descriptor=model_descriptor,
        parent_otel_span=parent_otel_span,
        sibling_priorities=sibling_priorities,
        saturation_threshold=_get_priority_settings().saturation_threshold,
    )
    return htb_response
```

- **`_create_priority_based_descriptors`** (`dynamic_rate_limiter_v3.py:188`) builds the per-priority descriptor. It calls `_normalize_priority_weights` (`:151`) to handle weights that sum to >1.0, then `_get_priority_allocation` (`:158`) to pick the pool key (`{model}:{priority}` for explicit priorities, `{model}:default_pool` for keys without explicit priority).
- **`_create_model_tracking_descriptor`** (`dynamic_rate_limiter_v3.py:236`) builds the model-wide descriptor with `high_limit_multiplier=1` (so the model cap equals the configured RPM).
- **`_get_sibling_priorities`** (`dynamic_rate_limiter_v3.py:283`) returns `List[tuple[str, int]]` — `(sibling_priority_key, guaranteed_rpm)` for every priority in `litellm.priority_reservation` except the current one.

**`_get_sibling_priorities`** — `dynamic_rate_limiter_v3.py:283`:

```python
def _get_sibling_priorities(self, model, model_group_info, current_priority) -> List[tuple[str, int]]:
    if litellm.priority_reservation is None or model_group_info.rpm is None:
        return []
    normalized_weights = self._normalize_priority_weights(model_group_info)
    siblings: List[tuple[str, int]] = []
    for prio_key in litellm.priority_reservation:
        if prio_key == current_priority:
            continue
        weight = normalized_weights.get(prio_key, 0.0)
        guaranteed_rpm = int(model_group_info.rpm * weight)
        sibling_priority_key = f"{model}:{prio_key}"
        siblings.append((sibling_priority_key, guaranteed_rpm))
    return siblings
```

- Sibling keys use the same `{model}:{priority}` format as the request counter, so the Lua script can build the demand keys `{htb:{model}}:{model}:{priority}:demand:requests` and find them.
- `guaranteed_rpm` is the reservation the Lua script will hold against the borrow ceiling: `reservation = min(sibling_demand, guaranteed_rpm)`.

### 1.5 The atomic Lua check-and-increment

**`htb_check_and_increment`** — `litellm/proxy/hooks/parallel_request_limiter_v3.py:1040` (method body), builds keys/args then dispatches.

**Key construction** (lines 1071-1085):

```python
htb_hash = f"htb:{model_descriptor['value']}"
priority_suffix = priority_descriptor["value"]          # e.g. "model:prior1" or "model:default_pool"
priority_window_key   = f"{{{htb_hash}}}:{priority_suffix}:window"
priority_counter_key  = f"{{{htb_hash}}}:{priority_suffix}:requests"
model_window_key      = f"{{{htb_hash}}}:window"
model_counter_key     = f"{{{htb_hash}}}:requests"
my_demand_window_key   = f"{{{htb_hash}}}:{priority_suffix}:demand:window"
my_demand_counter_key  = f"{{{htb_hash}}}:{priority_suffix}:demand:requests"
```

**Saturation cap** (line 1088): `saturation_cap = int(model_limit * saturation_threshold)`.

**Demand counter increment (before the lock/Lua)** — `parallel_request_limiter_v3.py:1124`:

```python
await self._increment_demand_counter(
    my_demand_window_key, my_demand_counter_key, window_size, ttl, parent_otel_span,
)
```

`_increment_demand_counter` (`parallel_request_limiter_v3.py:1158`) is a sliding-window increment that writes with **`local_only=False`** so the demand counter reaches Redis, where the Lua script's sibling demand reads (`redis.call('GET', ...)`) can see it across pods. The window-active path uses `async_increment_cache` (atomic Redis `INCR`) instead of a read-then-write, eliminating the cross-pod lost-increment race. When Redis is absent, `DualCache` degrades to in-memory-only writes, preserving single-instance behavior. See "Known issues" below for the residual window-boundary race.

**Dispatch** (lines 1146-1180): if `self.htb_check_and_increment_script is not None` (Redis present), call the Lua script; otherwise fall back to `_htb_in_memory` under `self._check_and_increment_lock`.

### 1.6 The Lua script

**`HTB_CHECK_AND_INCREMENT_SCRIPT`** — `litellm/proxy/hooks/parallel_request_limiter_v3.py:183` (full source verified). The decision logic (lines 302-311):

```lua
-- DENY checks:
-- 1. Borrowing and priority has exceeded its borrow ceiling
-- 2. Model is at total capacity (hard limit, cannot be exceeded)
if priority_current >= priority_limit and priority_current >= borrow_ceiling then
    return { 1, priority_current, priority_limit, 0 }
end
if model_current >= model_limit then
    return { 1, priority_current, priority_limit, 0 }
end

local borrowed = 0
if priority_current >= priority_limit then
    borrowed = 1
end

local new_priority = increment_counter(priority_window, priority_counter_key, priority_window_expired, ttl, window_size)
local new_model    = increment_counter(model_window, model_counter_key, model_window_expired, ttl, window_size)
return { 0, new_priority, new_model, borrowed }
```

**Borrow ceiling** (lines 279-296):

```lua
local borrow_ceiling = math.min(saturation_cap, model_limit)
local arg_idx = 8
local key_idx = 7
for i = 1, num_siblings do
    local sib_demand_window_key  = KEYS[key_idx]
    local sib_demand_counter_key = KEYS[key_idx + 1]
    local sibling_guaranteed = tonumber(ARGV[arg_idx])
    local sib_demand = read_counter(sib_demand_window_key, sib_demand_counter_key)
    local reservation = math.min(sib_demand, sibling_guaranteed)
    borrow_ceiling = borrow_ceiling - reservation
    arg_idx = arg_idx + 1
    key_idx = key_idx + 2
end
if borrow_ceiling < priority_limit then
    borrow_ceiling = priority_limit
end
```

**KEYS layout** (6 + 2*num_siblings):
- `KEYS[1]` priority window, `KEYS[2]` priority counter (accepted)
- `KEYS[3]` model window, `KEYS[4]` model counter (accepted)
- `KEYS[5]`, `KEYS[6]` own demand window/counter (reserved for backward compat; the demand counter is incremented separately in Python before the script)
- `KEYS[7..]` per sibling: `(demand_window_key, demand_counter_key)`, **read only**

**ARGV layout**: `[priority_limit, model_limit, ttl, window_size, num_siblings, saturation_cap, 0, sibling_guaranteed_rates...]`.

**Return shape**: `{ status_code, value1, value2, borrowed_flag }` where `status_code` 0 = OK, 1 = OVER_LIMIT. `_build_htb_response` (`parallel_request_limiter_v3.py:1216`) converts this to a `RateLimitResponse`.

### 1.7 In-memory fallback

**`_htb_in_memory`** — `parallel_request_limiter_v3.py:1263`. Mirrors the Lua logic exactly (same borrow-ceiling math, same demand-based reservation, same deny conditions) but uses `local_only=True` cache reads/writes and is serialized by `self._check_and_increment_lock`. Called when Redis is absent or the Lua script raises.

### 1.8 Flat RPM check bypass

**`ModelRateLimitingCheck.async_pre_call_check`** — `litellm/router_utils/pre_call_checks/model_rate_limit_check.py:163`:

```python
async def async_pre_call_check(self, deployment, parent_otel_span=None) -> Optional[Dict]:
    try:
        tpm_limit, rpm_limit = self._get_deployment_limits(deployment)
        if tpm_limit is None and rpm_limit is None:
            return deployment

        from litellm.proxy.hooks.dynamic_rate_limiter_v3 import htb_priority

        if htb_priority.get() is not None:
            return deployment
        ...
```

- **Skip at line 178:** `if htb_priority.get() is not None: return deployment`.
- The **sync `pre_call_check`** at `model_rate_limit_check.py:102` has the identical skip.
- Only `htb_priority` is read; `htb_approved` does not exist.
- When `priority_reservation` is not configured, `async_pre_call_hook` never sets `htb_priority`, so `.get()` returns `None` and the flat check runs as normal. This makes the change fully backward-compatible.

### 1.9 Raising the rate limit error

**`_raise_rate_limit_error`** — `dynamic_rate_limiter_v3.py:347`. Builds a `litellm.RateLimitError` with `llm_provider` and `model` resolved via `resolve_llm_provider_for_rate_limit`, an `httpx.Response(status_code=429, ...)`, and `retry-after` set to the window size. The router catches this, cools down the deployment, and tries the next fallback.

### 1.10 Post-call / teardown

**No HTB counter decrement exists.** The priority/model/demand counters only expire at window reset (via TTL). This is intentional: HTB is a request-rate limiter, not a concurrency gauge.

- `async_log_success_event` (`dynamic_rate_limiter_v3.py:468`) **increments** token counters for `model_saturation_check` and `priority_model` (TPM tracking). These token keys are separate from the HTB Lua script's request-counter keys; token tracking is post-call accounting, the Lua script is pre-call enforcement.
- `async_post_call_success_hook` (`dynamic_rate_limiter_v3.py:441`) adds `x-litellm-priority` and `x-litellm-rate-limiter-version` response headers.
- `async_log_failure_event` (`parallel_request_limiter_v3.py:3158`) decrements `max_parallel_requests` and refunds TPM reservation but **does not touch HTB priority/model/demand counters**.
- `async_release_max_parallel_requests_on_disconnect` (`parallel_request_limiter_v3.py:3244`) releases the `max_parallel_requests` slot on stream cancel; again, no HTB counter refund.

---

## Redis Key Layout (verified)

All keys for a model share the `{htb:<model>}` hash tag for Redis Cluster co-location.

```
{htb:<model>}:window                              # model-wide window start
{htb:<model>}:requests                            # model-wide accepted request count

{htb:<model>}:<model>:<priority>:window           # priority window start
{htb:<model>}:<model>:<priority>:requests         # priority accepted request count
{htb:<model>}:<model>:<priority>:demand:window    # priority demand window start
{htb:<model>}:<model>:<priority>:demand:requests  # priority demand count (attempted)

{htb:<model>}:<model>:default_pool:window         # default-pool window start
{htb:<model>}:<model>:default_pool:requests       # default-pool accepted request count
{htb:<model>}:<model>:default_pool:demand:window
{htb:<model>}:<model>:default_pool:demand:requests
```

The `<model>:<priority>` suffix is `priority_descriptor["value"]` (built in `_get_priority_allocation` at `dynamic_rate_limiter_v3.py:158`). For keys without an explicit priority, the suffix is `<model>:default_pool` and the weight is `PriorityReservationSettings.default_priority`.

---

## Configuration Surface

| Setting | Where defined | Default | Effect |
|---|---|---|---|
| `litellm.priority_reservation` | `litellm/__init__.py:464` | `None` | Dict mapping priority name to weight. When `None`, HTB is fully disabled (the hook returns early, the flat check runs). |
| `PriorityReservationSettings.default_priority` | `litellm/types/utils.py:3716` | `0.25` | Weight for keys without explicit priority; they share the `default_pool`. |
| `PriorityReservationSettings.saturation_threshold` | `litellm/types/utils.py:3721` | `1.0` | `saturation_cap = model_limit * saturation_threshold`. Caps borrow headroom. |
| `window_size` | `self.v3_limiter.window_size` | 60s | Sliding-window length and counter TTL. |
| `PROXY_HOOKS["dynamic_rate_limiter_v3"]` | `litellm/proxy/hooks/__init__.py:46` | registered | Loads `_PROXY_DynamicRateLimitHandlerV3` as a proxy hook. |
| `optional_pre_call_checks: [enforce_model_rate_limits]` | router config | off | Registers `ModelRateLimitingCheck` so its skip logic runs alongside HTB. |

Priority names are arbitrary (`prior1`, `gold`, etc.); the code iterates `litellm.priority_reservation` keys dynamically. Weights are normalized if they sum to >1.0 (`_normalize_priority_weights`, `dynamic_rate_limiter_v3.py:151`).

---

## Data Contracts

### Proxy hook input/output
- **Input:** `UserAPIKeyAuth` (with `team_metadata` and `metadata`), `data: dict` (must contain `"model"`).
- **Output:** `None` (no modification to the request). Side effect: `htb_priority` ContextVar is set.

### Router pre-call check input/output
- **Input:** `deployment: dict` (must have `model_name`), `parent_otel_span`.
- **Output:** `deployment` (unchanged) on ALLOW; raises `litellm.RateLimitError` on OVER_LIMIT.

### Lua script input/output
- **Input:** KEYS = `[priority_window, priority_counter, model_window, model_counter, own_demand_window, own_demand_counter, *sibling_demand_pairs]`; ARGV = `[priority_limit, model_limit, ttl, window_size, num_siblings, saturation_cap, 0, *sibling_guaranteed_rates]`.
- **Output:** `{0, new_priority_count, new_model_count, borrowed_flag}` on ALLOW; `{1, current_priority_count, priority_limit, 0}` on OVER_LIMIT.

---

## Known Issues (verified against code)

### 1. Multi-instance demand-counter bug (HIGH) — FIXED

**Was:** `_increment_demand_counter` wrote the demand counter with `local_only=True`, bypassing Redis. The Lua script reads sibling demand from Redis via `redis.call('GET', sib_demand_counter_key)`, so in a multi-pod deployment every sibling's demand read as `0`. `borrow_ceiling` degraded to `min(saturation_cap, model_limit)` with no sibling reservation, and guaranteed rates were not enforced across pods.

**Fix (Option 1):** `_increment_demand_counter` now writes with `local_only=False`, so the demand counter reaches Redis where the Lua script can read it. The increment path uses `async_increment_cache` (atomic Redis `INCR`) instead of a read-then-write, eliminating the cross-pod lost-increment race. `DualCache.async_set_cache` / `async_increment_cache` already guard with `if self.redis_cache is not None and local_only is False`, so single-pod and test environments (no Redis) gracefully degrade to in-memory-only writes, preserving the existing single-instance behavior.

**Why Option 2 (move increment into the Lua script) was rejected:** the demand counter uses an intentional two-phase design. Phase 1 (before the lock): each priority increments its demand and yields via `asyncio.sleep(0)` so concurrent priorities interleave. Phase 2 (inside the lock/Lua): the atomic check reads sibling demand declared in phase 1. Moving the increment inside the Lua script collapses both phases into one atomic step, so the first-scheduled priority reads sibling demand as `0` (siblings haven't declared yet) and grabs all capacity. This was confirmed by a failing test: `TestHTBInMemoryConcurrency::test_over_capacity_each_priority_gets_guaranteed_rate` saw prior2 get 0 RPM (starvation). The two-phase pre-increment with `asyncio.sleep(0)` interleaving is load-bearing engineering, not an accident.

**Regression tests:** `TestDemandCounterMultiPodVisibility` in `tests/test_litellm/proxy/hooks/test_priority_reservation_adeo.py` verifies (a) the demand counter write reaches Redis when Redis is configured, and (b) the window-active increment uses atomic `INCR` with `local_only=False`.

**Where it works:** single-instance deployments (in-memory fallback path reads demand via `local_only=True` cache, which is the same process, so sibling awareness works).

### 2. Stale docstring referencing EWMA — FIXED

`PriorityReservationSettings.saturation_threshold` docstring at `litellm/types/utils.py:3722` was updated by commit `428c128335` to reference demand-counter-based sibling reservation (`reservation = min(sibling_demand, sibling_guaranteed)`) instead of the removed EWMA approach. No action needed.

### 3. No counter refund on failure

Denied requests still count toward the demand counter (incremented before the Lua check). Allowed-but-failed requests still count toward the priority/model counters (incremented inside the Lua script on ALLOW). There is no refund path. This is intentional for request-rate limiting but worth noting for capacity planning.

### 4. Sync `pre_call_check` is a no-op

`_PROXY_DynamicRateLimitHandlerV3.pre_call_check` (`dynamic_rate_limiter_v3.py:432`) returns `deployment` without doing anything. If any code path calls the sync variant instead of `async_pre_call_check`, HTB enforcement is silently skipped. The router's `async_routing_strategy_pre_call_checks` calls the async variant, so this is not currently exercised, but it is a latent footgun.

---

## File-to-Function Index

| Function | File | Line |
|---|---|---|
| `htb_priority` ContextVar declaration | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 37 |
| `_get_priority_settings` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 44 |
| `_PROXY_DynamicRateLimitHandlerV3.__init__` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 72 |
| `_get_priority_weight` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 89 |
| `_get_priority_from_user_api_key_dict` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 110 |
| `_normalize_priority_weights` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 151 |
| `_get_priority_allocation` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 158 |
| `_create_priority_based_descriptors` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 188 |
| `_create_model_tracking_descriptor` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 236 |
| `_get_sibling_priorities` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 283 |
| `_run_htb_check` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 310 |
| `_raise_rate_limit_error` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 347 |
| `async_pre_call_hook` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 382 |
| `async_pre_call_check` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 395 |
| `pre_call_check` (sync no-op) | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 432 |
| `async_post_call_success_hook` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 441 |
| `async_log_success_event` | `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | 468 |
| `HTB_CHECK_AND_INCREMENT_SCRIPT` | `litellm/proxy/hooks/parallel_request_limiter_v3.py` | 183 |
| `htb_check_and_increment` | `litellm/proxy/hooks/parallel_request_limiter_v3.py` | 1040 |
| `_increment_demand_counter` | `litellm/proxy/hooks/parallel_request_limiter_v3.py` | 1158 |
| `_build_htb_response` | `litellm/proxy/hooks/parallel_request_limiter_v3.py` | 1216 |
| `_htb_in_memory` | `litellm/proxy/hooks/parallel_request_limiter_v3.py` | 1263 |
| `ModelRateLimitingCheck.pre_call_check` (sync) | `litellm/router_utils/pre_call_checks/model_rate_limit_check.py` | 102 |
| `ModelRateLimitingCheck.async_pre_call_check` | `litellm/router_utils/pre_call_checks/model_rate_limit_check.py` | 163 |
| `async_routing_strategy_pre_call_checks` | `litellm/router.py` | 7204 |
| `PROXY_HOOKS` registration | `litellm/proxy/hooks/__init__.py` | 46 |
| `PriorityReservationSettings` | `litellm/types/utils.py` | 3713 |
| `DualCache.async_set_cache` | `litellm/caching/dual_cache.py` | 351 |

---

## How This Map Was Verified

Per the logic mapping technique's Phase 2, each step above was checked against the real source on branch `jya0-v1.92.0`:

1. **Lua script** read in full (`parallel_request_limiter_v3.py:183-311`) — decision logic, borrow ceiling, key/argv layout confirmed verbatim.
2. **Demand counter path** read in full (`parallel_request_limiter_v3.py:1060-1300`) — original `local_only=True` writes identified as the multi-instance bug; fixed to `local_only=False` with atomic `async_increment_cache`. Redis write-back confirmed via `DualCache.async_set_cache` / `async_increment_cache` (`dual_cache.py:351`, `dual_cache.py:380`).
3. **In-memory fallback** read in full (`parallel_request_limiter_v3.py:1300-1410`) — mirrors Lua logic exactly.
4. **Proxy hook and pre-call check** read in full (`dynamic_rate_limiter_v3.py:1-560`) — ContextVar name, early-return conditions, error-raising path confirmed.
5. **Flat-check bypass** read in full (`model_rate_limit_check.py:95-200`) — `htb_priority.get() is not None` skip confirmed for both sync and async.
6. **Router invocation** read in full (`router.py:7204-7270`) — callback iteration, cooldown, re-raise confirmed; call sites enumerated via grep (28 matches).
7. **Post-call teardown** confirmed absent of any HTB counter decrement via grep of `async_log_success_event` / `async_log_failure_event` in `dynamic_rate_limiter_v3.py` and `parallel_request_limiter_v3.py`.

This map supersedes the EWMA-era description in the user memory note `htb-rate-limiting.md`, which describes the pre-demand-counter implementation.
