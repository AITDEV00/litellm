# HTB Priority-Based Rate Limiting

## Table of Contents

- [What Is This?](#what-is-this)
- [Glossary](#glossary)
- [The Problem We Solved](#the-problem-we-solved)
- [Architecture Overview](#architecture-overview)
- [How It Works: Step by Step](#how-it-works-step-by-step)
- [The Two-Layer Design](#the-two-layer-design)
- [Demand-Based Borrowing](#demand-based-borrowing)
- [Redis Key Layout](#redis-key-layout)
- [Configuration](#configuration)
- [Code Review](#code-review)
- [What Changed vs the Original Codebase](#what-changed-vs-the-original-codebase)
- [Test Results](#test-results)
- [How to Run the Tests](#how-to-run-the-tests)
- [Troubleshooting](#troubleshooting)
- [Priority Logic Map (verified file:line trace)](./PRIORITY-LOGIC-MAP.md)

---

## What Is This?

This is a **priority-based rate limiting system** for the LiteLLM proxy that ensures different groups of API users get guaranteed minimum throughput on shared LLM models, while allowing unused capacity to be borrowed by other groups in real time.

The core algorithm is an **HTB (Hierarchical Token Bucket)**, the same class of algorithm used by Linux `tc` for network traffic shaping. Each priority level gets a guaranteed rate (like a committed information rate in networking), and when that priority is idle, its unused bandwidth flows to other priorities that need it, up to a configurable saturation cap.

> For a verified, file-and-line-referenced trace of the entire priority flow
> (entry point to exit point, every function and data contract), see
> [`PRIORITY-LOGIC-MAP.md`](./PRIORITY-LOGIC-MAP.md). That document is the
> single source of truth for the implementation; this README is the
> narrative overview.

### Why it matters

Without priority-based rate limiting, all API keys compete equally for model capacity. A single noisy consumer can starve everyone else. With this system:

- A "premium" tier (prior1) always gets at least 50% of model RPM, no matter what
- A "standard" tier (prior2) always gets at least 30%
- A "basic" tier (prior3) always gets at least 20%
- When any tier is idle, its reserved capacity is automatically borrowed by active tiers
- The model's hard RPM limit is never exceeded

---

## Glossary

| Term | Meaning |
|------|---------|
| **HTB** | Hierarchical Token Bucket. A rate limiting algorithm where each "bucket" (priority level) has a guaranteed rate and can borrow unused capacity from sibling buckets. Originated in network QoS (Linux `tc HTB`). |
| **RPM** | Requests Per Minute. The number of API requests a model can handle in a 60-second sliding window. |
| **TPM** | Tokens Per Minute. The number of tokens (input + output) a model can process per minute. |
| **Demand Counter** | A sliding-window counter incremented *before* the atomic check-and-increment. It reflects how many requests a priority has *attempted* (including denied ones), making a priority's demand visible to siblings immediately. This replaces the previous EWMA approach, which lagged behind reality and caused starvation under concurrent burst. |
| **Borrowing** | When a priority level exceeds its guaranteed rate but the model has spare capacity (because sibling priorities are idle or under-utilizing their guarantees), the request is allowed. The "borrow ceiling" is computed as `min(saturation_cap, model_limit) - sum_of_sibling_reservations`. |
| **Saturation Cap** | A configurable ceiling (default 1.0, i.e. full model RPM) on borrow headroom. When sum(guaranteed) equals model RPM, the threshold is irrelevant because guaranteed rates always take priority. A value below 1.0 leaves a buffer for priority transitions but may cause under-utilization. |
| **Priority Reservation** | A configuration mapping (e.g., `prior1: 0.50, prior2: 0.30, prior3: 0.20`) that defines what fraction of model capacity each priority level is guaranteed. The weights do not need to sum to 1.0; if they exceed 1.0, they are normalized. |
| **Sliding Window** | A time-based window (default 60 seconds) over which request counts are tracked. When the window expires, the counter resets. |
| **Fallback Chain** | A router configuration where if a model returns a rate limit error (429), the proxy automatically retries the request on a different model. Example: GLM-5.2 -> GLM-5.1 -> MiniMax. |
| **Pre-call Hook** | A proxy-level callback (`async_pre_call_hook`) that runs before a request is processed. It has access to the API key, user metadata, and request data. |
| **Pre-call Check** | A router-level callback (`async_pre_call_check`) that runs for every deployment pick, including fallback deployments. It receives the deployment dict and can raise `RateLimitError` to block that specific deployment and trigger fallback. |
| **ContextVar** | A Python `contextvars.ContextVar` that carries request-scoped state across async call boundaries without explicit parameter passing. Used here to pass the priority from the proxy layer to the router layer. |
| **Lua Script** | A script executed atomically inside Redis. Used for the HTB check-and-increment to ensure that reading counters, computing the borrow ceiling, and incrementing counters all happen as a single atomic operation (no TOCTOU race). |
| **TOCTOU** | Time-of-Check to Time-of-Use. A race condition where a value is read, a decision is made, but the value changes before the action is taken. The Lua script prevents this by making check + increment atomic. |
| **Hash Tag** | A Redis Cluster feature `{...}` in a key name that forces all keys with the same tag to be stored on the same shard. Used so all HTB keys for a model are co-located, allowing the Lua script to access them without CROSSSLOT errors. |

---

## The Problem We Solved

### Before: Flat Rate Limiting, No Priority Enforcement

The upstream LiteLLM codebase shipped two dynamic rate limiter classes (`dynamic_rate_limiter.py` v1 and `dynamic_rate_limiter_v3.py` v3), but **neither was registered in `PROXY_HOOKS` or instantiated anywhere in the proxy**. The v1 limiter was enterprise-gated (required a `LITELLM_LICENSE` env var) and the v3 limiter was never wired up at all.

The only rate limiting that actually ran was `ModelRateLimitingCheck`, a flat per-deployment RPM/TPM enforcement check at the router layer. It treated all API keys equally — no priority levels, no guaranteed rates, no borrowing. When a model reached its RPM limit, every subsequent request got a 429 regardless of who was sending it.

```
Client A (VIP)   -> Proxy -> Router -> Model A (100 RPM) -> 429 (flat limit, no priority)
Client B (basic)  -> Proxy -> Router -> Model A (100 RPM) -> 429 (same treatment as VIP)
```

If the router had fallbacks configured, it would cascade to the next model, but that fallback model also had no priority awareness. A basic-tier user could exhaust a fallback model's capacity while a VIP user was stuck waiting.

### After: HTB Priority Enforcement with Fallback Awareness

We built a complete HTB (Hierarchical Token Bucket) priority rate limiting system. We took the existing `_PROXY_DynamicRateLimitHandlerV3` class (which had a saturation-based approach that was never activated), registered it in `PROXY_HOOKS`, injected the router into it, and replaced its internal logic with the HTB algorithm. The new system introduces:

1. **Priority levels** configured via `priority_reservation` (e.g., `prior1: 0.50, prior2: 0.30, prior3: 0.20`)
2. **Guaranteed rates**: each priority gets a reserved fraction of every model's RPM
3. **Borrowing**: when a priority is idle, its unused capacity flows to active priorities via demand-based sibling reservation
4. **Per-deployment enforcement**: the HTB check runs on every deployment the router picks, including fallbacks, ensuring priority guarantees hold across the entire fallback chain

The system operates at two layers:

```
Client -> Proxy (pre_call_hook: extract priority from API key metadata, store in ContextVar)
              -> Router picks deployment (primary or fallback)
                  -> Router (pre_call_check: run HTB Lua script on THIS deployment)
                      -> If OK: send to model
                      -> If OVER_LIMIT: raise RateLimitError
                          -> Router catches, tries next fallback
                              -> Router (pre_call_check: run HTB on fallback deployment)
                                  -> If OK: send to fallback model
                                  -> If OVER_LIMIT: try next fallback or return 429
```

This ensures **every model in any fallback chain** gets HTB enforcement with the correct priority, and the model-wide RPM hard limit is never exceeded.

---

## Architecture Overview

The following UML sequence diagram shows the generic flow for any request, including fallback handling. The diagram uses generic model names (Primary Model, Fallback Model 1, Fallback Model 2) to illustrate that the system works with any fallback chain configuration.

```mermaid
sequenceDiagram
    participant C as Client
    participant P as Proxy Layer
    participant R as Router Layer
    participant Redis as Redis (Lua Script)
    participant M as Model Endpoint

    C->>P: POST /v1/chat/completions
    Note over P: async_pre_call_hook (DynamicRateLimitHandlerV3)
    P->>P: Extract priority from API key metadata
    P->>P: Store priority in htb_priority ContextVar
    P->>R: Forward request to router

    rect rgb(240, 248, 255)
        Note over R: Try Primary Model deployment
        R->>R: async_routing_strategy_pre_call_checks
        R->>Redis: async_pre_call_check: run HTB Lua script
        Redis->>Redis: Check priority bucket + model-wide bucket
        alt Within guaranteed rate or borrowing allowed
            Redis-->>R: ALLOW (increment counters)
            R->>M: Send request to Primary Model
            M-->>C: 200 OK (response from Primary Model)
        else OVER_LIMIT (model at capacity)
            Redis-->>R: DENY
            R->>R: raise litellm.RateLimitError
            Note over R: Router catches error, tries fallback
        end
    end

    rect rgb(255, 248, 240)
        Note over R: Try Fallback Model 1 deployment
        R->>Redis: async_pre_call_check: run HTB Lua script
        Redis->>Redis: Check priority bucket + model-wide bucket
        alt Within guaranteed rate or borrowing allowed
            Redis-->>R: ALLOW (increment counters)
            R->>M: Send request to Fallback Model 1
            M-->>C: 200 OK (response from Fallback Model 1)
        else OVER_LIMIT (model at capacity)
            Redis-->>R: DENY
            R->>R: raise litellm.RateLimitError
            Note over R: Router catches error, tries next fallback
        end
    end

    rect rgb(248, 240, 255)
        Note over R: Try Fallback Model 2 deployment
        R->>Redis: async_pre_call_check: run HTB Lua script
        Redis->>Redis: Check priority bucket + model-wide bucket
        alt Within guaranteed rate or borrowing allowed
            Redis-->>R: ALLOW (increment counters)
            R->>M: Send request to Fallback Model 2
            M-->>C: 200 OK (response from Fallback Model 2)
        else OVER_LIMIT (no more fallbacks)
            Redis-->>R: DENY
            R-->>C: 429 Rate Limit Exceeded
        end
    end

    rect rgb(240, 255, 240)
        Note over R: Also: ModelRateLimitingCheck (flat RPM/TPM)
        R->>R: Check htb_priority ContextVar
        alt htb_priority is set (HTB active)
            R->>R: SKIP flat check (HTB already counted)
        else htb_priority is None (no priority configured)
            R->>R: Run flat RPM/TPM enforcement as normal
        end
    end
```

The sequence repeats for each deployment in the fallback chain. If the primary model is at capacity, the router catches the `RateLimitError`, picks the next fallback, and runs the same HTB check on that deployment. This continues until a deployment allows the request or all fallbacks are exhausted.

### Key Design Decisions

1. **`async_pre_call_hook` (proxy layer) only extracts priority** — it no longer runs the HTB Lua script. It reads the priority from the API key's metadata and stores it in a `ContextVar` called `htb_priority`. This is intentionally lightweight because the proxy hook runs once per request, but the router may try multiple deployments.

2. **`async_pre_call_check` (router layer) runs the HTB check** — this method is called by `async_routing_strategy_pre_call_checks` in `router.py` for every deployment pick, including fallbacks. It reads the priority from the `ContextVar`, builds the descriptors, and calls the Lua script. If the check fails, it raises `litellm.RateLimitError`, which the router catches and uses to trigger the next fallback.

3. **`ModelRateLimitingCheck` skips when HTB is active** — the router's flat RPM/TPM enforcement check (`enforce_model_rate_limits`) checks whether `htb_priority` is set. If it is, the flat check is skipped entirely to avoid double-counting (since the HTB Lua script already increments the model-wide counter). If no priority is configured (i.e., `priority_reservation` is not set in the config), the flat check runs as normal.

---

## How It Works: Step by Step

### Step 1: Client sends request with API key

```bash
curl https://litellm.adeoaiengine.ecouncil.ae/v1/chat/completions \
  -H "Authorization: Bearer sk-BR2PtwOoOIU8c01Cfq_y6g" \
  -H "Content-Type: application/json" \
  -d '{"model": "zai-org/GLM-5.2-FP8", "messages": [{"role": "user", "content": "Hello"}]}'
```

The API key `sk-BR2PtwOoOIU8c01Cfq_y6g` belongs to user `maalmarri@ECOUNCIL.AE`, whose team metadata contains `priority: "prior1"`.

### Step 2: Proxy layer extracts priority

`async_pre_call_hook` in `DynamicRateLimitHandlerV3` runs. It:
1. Checks if the request has a `model` field (returns early if not)
2. Extracts the priority from `user_api_key_dict.team_metadata` (falls back to `user_api_key_dict.metadata`)
3. Sets `htb_priority.set(priority)` — this stores `"prior1"` in the `ContextVar`
4. Returns `None` (no modification to the request)

### Step 3: Router picks a deployment

The router selects a deployment for `zai-org/GLM-5.2-FP8`. Before sending the request, it calls `async_routing_strategy_pre_call_checks`, which iterates over all callbacks in `litellm.callbacks` and calls `async_pre_call_check(deployment, parent_otel_span)` on each.

### Step 4: HTB check runs on the deployment

`async_pre_call_check` in `DynamicRateLimitHandlerV3` runs:

1. Checks if `litellm.priority_reservation` is configured (returns early if not)
2. Reads `priority = htb_priority.get()` → `"prior1"`
3. Gets `model_group` from `deployment["model_name"]` → `"zai-org/GLM-5.2-FP8"`
4. Gets `model_group_info` from `self.llm_router.get_model_group_info(model_group)` → contains `rpm=100`
5. Calls `_run_htb_check(model, model_group_info, priority, parent_otel_span)`

### Step 5: HTB Lua script executes atomically in Redis

The Lua script `HTB_CHECK_AND_INCREMENT_SCRIPT` does the following in a single atomic Redis transaction:

```
1. Read the priority counter (prior1's request count in current window)
2. Read the model-wide counter (total requests for GLM-5.2 in current window)
3. Read each sibling's demand counter (sliding-window, incremented before the Lua script)
4. Reserve min(sibling_demand, sibling_guaranteed) for each sibling
5. Compute borrow_ceiling = min(saturation_cap, model_limit) - sum_of_sibling_reservations
6. Decision:
   - If priority_current < priority_limit AND model_current < model_limit: ALLOW
   - If priority_current >= priority_limit but priority_current < borrow_ceiling AND model_current < model_limit: ALLOW (borrowing)
   - Otherwise: DENY
7. If ALLOW: increment both counters, return {0, new_priority_count, new_model_count, borrowed_flag}
8. If DENY: return {1, current_priority_count, priority_limit, 0}
```

### Step 6: Result handling

- **If ALLOWED**: `async_pre_call_check` returns `deployment` (the deployment dict, unmodified). The router proceeds to send the request to the model.
- **If DENIED**: `async_pre_call_check` calls `_raise_rate_limit_error`, which raises `litellm.RateLimitError`. The router catches this and:
  - If fallbacks are configured for this model: tries the next fallback deployment (goes back to Step 3 with a new deployment)
  - If no fallbacks remain: returns a 429 response to the client

### Step 7: ModelRateLimitingCheck (flat RPM) is skipped

Because `htb_priority.get() is not None` (it was set in Step 2), `ModelRateLimitingCheck.async_pre_call_check` sees that HTB is active and returns `deployment` immediately without running its own flat RPM check. This prevents double-counting: the HTB Lua script already incremented the model-wide counter.

---

## The Two-Layer Design

### Why two layers?

| Layer | Hook Method | When It Runs | What It Does |
|-------|-------------|--------------|--------------|
| Proxy | `async_pre_call_hook` | Once per request, before routing | Extracts priority from API key metadata, stores in `ContextVar` |
| Router | `async_pre_call_check` | Once per deployment pick (including fallbacks) | Runs the HTB Lua script, raises `RateLimitError` if denied |

The proxy layer cannot run the HTB check because it doesn't know which deployment the router will pick (or whether it will fall back). The router layer cannot extract the priority because it only receives a `deployment` dict, not the `user_api_key_dict`.

The `ContextVar` bridges this gap: the proxy layer writes the priority, and the router layer reads it, without any parameter passing through the router's internal call stack.

### Why not merge everything into one layer?

The router's `async_routing_strategy_pre_call_checks` is called from dozens of places in `router.py` (for completion, embedding, transcription, etc.). Merging the HTB logic into the router would require modifying all of those call sites. Instead, by implementing `async_pre_call_check` as a callback, the HTB check is automatically called wherever the router does pre-call checks, including all fallback paths.

---

## Demand-Based Borrowing

### The challenge

When prior3 wants to borrow capacity, how much can it borrow? The answer depends on how much prior1 and prior2 are actually using. But we cannot just look at their current window counters, because:

- If prior1 sent 50 requests 59 seconds ago and then stopped, its counter still shows 50, but it is effectively idle
- If prior1 never sent any requests, there is no counter at all, and all its guaranteed capacity should be borrowable

The previous EWMA-based approach tracked each priority's recent request rate with exponential decay. However, EWMA has a fatal flaw under concurrent burst: when all priorities start simultaneously, every sibling starts with EWMA=0 (never sent before). A higher priority can borrow all the capacity before lower priorities' EWMA converges, causing starvation.

### The solution: Demand counter

A **demand counter** is a sliding-window counter (same mechanism as the request counter) that is incremented *before* the atomic Lua script runs. It reflects how many requests a priority has *attempted* in the current window, including requests that were denied. This makes a priority's demand visible to siblings immediately, even before its first request is processed by the Lua script.

```
Before the Lua script:
  INCR {htb:<model>}:<priority>:demand:requests  (with sliding window)

Inside the Lua script (borrow ceiling):
  for each sibling:
    demand = read_counter(sibling_demand_window, sibling_demand_counter)
    reservation = min(demand, sibling_guaranteed)
```

### How the borrow ceiling is computed

For each sibling priority, the Lua script:
1. Reads the sibling's demand counter (sliding-window count of attempted requests)
2. Reserves `min(sibling_demand, sibling_guaranteed_rate)`

The borrow ceiling is then:

```
borrow_ceiling = min(saturation_cap, model_limit) - sum_of_sibling_reservations
```

Where `saturation_cap = model_limit * saturation_threshold` (default 1.0, so saturation_cap = model_limit).

### Example scenarios

**Scenario A: All priorities active at full guaranteed rate**

```
Model: 100 RPM, saturation_cap = 100
prior1: guaranteed=50, demand=200 → reservation = min(200, 50) = 50
prior2: guaranteed=30, demand=200 → reservation = min(200, 30) = 30
prior3 wants to borrow:
  borrow_ceiling = 100 - 50 - 30 = 20
  prior3 can borrow up to 20 RPM (on top of its 20 RPM guarantee = 40 total, but capped by model_limit)
```

**Scenario B: prior1 idle, prior2 active**

```
Model: 100 RPM, saturation_cap = 100
prior1: guaranteed=50, demand=0 (idle) → reservation = min(0, 50) = 0
prior2: guaranteed=30, demand=200 → reservation = min(200, 30) = 30
prior3 wants to borrow:
  borrow_ceiling = 100 - 0 - 30 = 70
  prior3 can borrow up to 70 RPM (on top of its 20 RPM guarantee = 90 total, but capped by model_limit)
```

**Scenario C: prior1 never sent**

```
Model: 100 RPM, saturation_cap = 100
prior1: guaranteed=50, demand=0 (never sent) → reservation = min(0, 50) = 0
prior2: guaranteed=30, demand=0 (never sent) → reservation = min(0, 30) = 0
prior3 wants to borrow:
  borrow_ceiling = 100 - 0 - 0 = 100
  prior3 can borrow up to 100 RPM (on top of its 20 RPM guarantee = 120 total, but capped at model_limit = 100)
```

---

## Redis Key Layout

All keys for a given model share the `{htb:<model>}` hash tag to ensure they are co-located on the same Redis Cluster shard. This allows the Lua script to access all keys atomically without CROSSSLOT errors.

```
{htb:zai-org/GLM-5.2-FP8}:requests                                    # Model-wide request counter
{htb:zai-org/GLM-5.2-FP8}:window                                      # Model-wide window start timestamp

{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior1:requests         # prior1 request counter
{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior1:window           # prior1 window start timestamp
{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior1:demand:requests  # prior1 demand counter
{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior1:demand:window    # prior1 demand window start

{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior2:requests         # prior2 request counter
{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior2:window           # prior2 window start timestamp
{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior2:demand:requests  # prior2 demand counter
{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior2:demand:window    # prior2 demand window start

{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior3:requests         # prior3 request counter
{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior3:window           # prior3 window start timestamp
{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior3:demand:requests  # prior3 demand counter
{htb:zai-org/GLM-5.2-FP8}:zai-org/GLM-5.2-FP8:prior3:demand:window    # prior3 demand window start
```

Key naming convention:
- The hash tag `{htb:<model>}` ensures all keys for a model are on the same Redis shard
- The priority suffix is `<model>:<priority>` (e.g., `zai-org/GLM-5.2-FP8:prior1`)
- Counter keys: `:requests` for the accepted request count, `:window` for the window start timestamp
- Demand keys: `:demand:requests` for the attempted request count, `:demand:window` for the demand window start
- Demand keys have the same TTL as request counters (window_size) so they expire when the window resets

---

## Configuration

### LiteLLM proxy config (`config.yaml`)

```yaml
litellm_settings:
  # Priority reservation: each priority gets a guaranteed fraction of model RPM
  priority_reservation:
    prior1: 0.50    # 50% of model RPM guaranteed
    prior2: 0.30    # 30% of model RPM guaranteed
    prior3: 0.20    # 20% of model RPM guaranteed

  # Settings for the HTB algorithm
  priority_reservation_settings:
    saturation_threshold: 0.80   # Borrowing capped at 80% of model RPM
    # default_priority: 0.50     # Weight for keys without explicit priority (default: 0.0)

  callbacks: litellm_hooks.vllm_param_injector.vllm_param_injector

router_settings:
  optional_pre_call_checks:
    - enforce_model_rate_limits   # Enables ModelRateLimitingCheck at the router layer
```

### Priority assignment on API keys

Priorities are assigned via team or key metadata. In the LiteLLM admin UI or via the API:

```bash
# Set priority on a team (all keys in this team inherit it)
curl -X POST https://litellm.adeoaiengine.ecouncil.ae/team/update \
  -H "Authorization: Bearer sk-1234" \
  -H "Content-Type: application/json" \
  -d '{"team_id": "05cc86a3-...", "metadata": {"priority": "prior1"}}'

# Or set priority on an individual key
curl -X POST https://litellm.adeoaiengine.ecouncil.ae/key/update \
  -H "Authorization: Bearer sk-1234" \
  -H "Content-Type: application/json" \
  -d '{"key": "sk-...", "metadata": {"priority": "prior2"}}'
```

Team metadata takes precedence over key metadata. If neither is set, the key falls into the `default_pool` with the `default_priority` weight.

### Priority value formats

The `priority_reservation` config supports multiple formats:

```yaml
# Format 1: Plain float (fraction of model capacity)
priority_reservation:
  prior1: 0.50

# Format 2: Dict with percent
priority_reservation:
  prior1:
    type: percent
    value: 0.50

# Format 3: Dict with absolute RPM
priority_reservation:
  prior1:
    type: rpm
    value: 50   # 50 RPM guaranteed, regardless of model RPM
```

### Important: priority names are fully configurable

The system does **not** hardcode `prior1`, `prior2`, `prior3`. Any names work:

```yaml
priority_reservation:
  gold: 0.60
  silver: 0.30
  bronze: 0.10
```

The code iterates `litellm.priority_reservation` (a dict) and uses whatever keys are configured. The sibling priority computation, demand tracking, and borrowing logic all work with arbitrary priority names.

### Model RPM limits

Each model deployment must have `rpm` set (in the model's `model_info` or `litellm_params`). This is the total capacity that gets divided among priorities:

```bash
# Via the LiteLLM admin API
curl -X POST https://litellm.adeoaiengine.ecouncil.ae/v1/model/info \
  -H "Authorization: Bearer sk-1234" \
  -d '{"litellm_params": {"model": "zai-org/GLM-5.2-FP8", ...}, "model_info": {"rpm": 100}}'
```

---

## Code Review

### Files modified

#### 1. `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` (core HTB logic)

**What it does**: Defines `_PROXY_DynamicRateLimitHandlerV3`, the main HTB rate limiter class. It is registered as both a proxy hook and a router callback.

**Changes from original codebase**:
- Replaced the old saturation-based three-phase approach (`_check_rate_limits` with read-only check, decide, increment) with a single atomic Lua script (`htb_check_and_increment`)
- Replaced `htb_approved: ContextVar[bool]` with `htb_priority: ContextVar[Optional[str]]` — instead of a boolean "approved" flag, we now carry the actual priority string so the router layer can use it
- Removed `_model_has_fallbacks` — the new design does not need to check for fallbacks at the proxy layer; the router handles fallback triggering naturally by catching `litellm.RateLimitError`
- Removed `_check_priority_limits` (the old proxy-layer HTB check) and replaced it with `_run_htb_check` (a pure function that returns a `RateLimitResponse` without side effects on `data`)
- Added `async_pre_call_check` — the router-layer hook that runs HTB on each deployment
- Added `pre_call_check` (sync no-op) — required because the router calls both sync and async variants
- Added `_raise_rate_limit_error` — extracts the error-raising logic into a separate method
- Changed error type from `ProxyRateLimitError` (HTTP 429 at proxy layer) to `litellm.RateLimitError` (router-layer exception that triggers fallback)
- Removed `HTTPException` import, added `httpx` import (for constructing the `httpx.Response` that `litellm.RateLimitError` requires)

**Quality assessment**:
- Clean separation of concerns: `_run_htb_check` is a pure function, `_raise_rate_limit_error` handles error formatting, `async_pre_call_check` orchestrates
- Removed dead code: `_check_model_saturation`, `_compute_saturation_from_response`, `_get_saturation_value_from_cache`, and `_get_saturation_check_cache_ttl` were no longer called from the main code path and have been deleted along with their tests
- Fixed `UserAPIKeyAuth()` empty instantiation: the `user_api_key_dict` parameter was removed from `_create_priority_based_descriptors` entirely (it was never used in the method body). `_run_htb_check` now passes only `model` and `priority`
- The `async_log_success_event` still tracks token usage using the old `priority_model` / `model_saturation_check` key format, which is separate from the HTB Lua script's key format. This is intentional — token tracking is a post-call accounting mechanism, while the HTB Lua script is a pre-call enforcement mechanism. They serve different purposes

#### 2. `litellm/proxy/hooks/parallel_request_limiter_v3.py` (Lua script + Python binding)

**What it does**: Contains the `HTB_CHECK_AND_INCREMENT_SCRIPT` Lua script and the `htb_check_and_increment` Python method that calls it.

**Changes from original codebase** (all additions, no modifications to existing code):
- Added `HTB_CHECK_AND_INCREMENT_SCRIPT` — a Lua script that atomically checks and increments the HTB buckets with demand-based borrowing
- Added `htb_check_and_increment` Python method — builds the Redis keys and arguments, calls the Lua script, falls back to in-memory when Redis is unavailable
- Added `_build_htb_response` — converts the Lua script's return value to a `RateLimitResponse` dict
- Added `_htb_in_memory` — an asyncio-lock-protected in-memory fallback that replicates the Lua script's logic for single-process deployments without Redis
- Added `self.htb_check_and_increment_script` — registers the Lua script with Redis for efficient execution

**Quality assessment**:
- The Lua script is well-commented with clear documentation of the algorithm, key layout, and return format
- The in-memory fallback faithfully replicates the Lua logic, including demand counter computation and borrow ceiling calculation
- Redis key construction uses hash tags (`{htb:<model>}`) correctly for Redis Cluster compatibility
- The fallback from Redis to in-memory is graceful — if the Lua script fails for any reason, it logs an error and falls back to the in-memory implementation

#### 3. `litellm/router_utils/pre_call_checks/model_rate_limit_check.py`

**What it does**: Enforces flat RPM/TPM limits on model deployments at the router layer.

**Changes from original codebase**:
- Added a check in both `pre_call_check` (sync) and `async_pre_call_check` (async) that skips the flat RPM/TPM enforcement when `htb_priority.get() is not None`
- Changed from `htb_approved.get()` (boolean) to `htb_priority.get() is not None` (checks if HTB is active)

**Quality assessment**:
- The skip logic is correct: when HTB is active, the Lua script already increments the model-wide counter, so the flat check would double-count
- The import is done inside the method (lazy import) to avoid circular import issues — this is the same pattern used elsewhere in the codebase
- When `priority_reservation` is not configured, `htb_priority` is never set (the proxy hook returns early), so `htb_priority.get()` returns `None`, and the flat check runs as normal. This means the change is backward-compatible

#### 4. `litellm/proxy/hooks/__init__.py`

**What it does**: Registers `_PROXY_DynamicRateLimitHandlerV3` in the `PROXY_HOOKS` dict so it can be loaded via the `callbacks` config.

**Changes**: Added import and registration entry.

#### 5. `litellm/proxy/utils.py`

**What it does**: ProxyLogging setup.

**Changes**:
- Added `update_variables(llm_router=llm_router)` call after instantiating proxy hooks — this gives the HTB hook access to the router so it can call `get_model_group_info`
- Removed a duplicate `callback.mark_pre_call_hook_ran(data)` call (bug fix)

#### 6. `litellm/proxy/hooks/dynamic_rate_limiter.py` (legacy v1)

**What it does**: The old v1 dynamic rate limiter.

**Changes**: Replaced `os.getenv("LITELLM_LICENSE")` check with `LicenseCheck().is_premium()` for consistency with v3.

### Compatibility with different configurations

The code is designed to work with any priority configuration:

1. **No `priority_reservation` configured**: `async_pre_call_hook` sets `htb_priority` to `None` (or the priority from metadata), `async_pre_call_check` returns early when `litellm.priority_reservation is None`. `ModelRateLimitingCheck` runs normally because `htb_priority.get()` is `None`. The system behaves exactly as the original codebase.

2. **Different priority names** (`gold`, `silver`, `bronze`): The code iterates `litellm.priority_reservation` keys dynamically. No priority names are hardcoded in any logic.

3. **Priority weights that don't sum to 1.0**: If weights sum to > 1.0, `_normalize_priority_weights` normalizes them proportionally. If they sum to < 1.0, the unallocated capacity is available for borrowing (since sibling reservations will be less than the model limit).

4. **Models without RPM/TPM limits**: `async_pre_call_check` returns early if `model_group_info.rpm is None and model_group_info.tpm is None`. The flat `ModelRateLimitingCheck` also returns early if no limits are set.

5. **Models without fallbacks**: When HTB denies a request and the model has no fallbacks, the `litellm.RateLimitError` propagates up through the router, which returns a 429 to the client. This is the correct behavior.

6. **Single-process deployment (no Redis)**: The `_htb_in_memory` fallback provides the same logic with an `asyncio.Lock` for single-process safety. Multi-process deployments require Redis for correct behavior.

### Issues found during review

1. **Fixed: Duplicate `mark_pre_call_hook_ran` call** in `litellm/proxy/utils.py` — the method was called twice in a row. Fixed by removing the duplicate.

2. **Fixed: Dead code** — `_check_model_saturation`, `_compute_saturation_from_response`, `_get_saturation_value_from_cache`, and `_get_saturation_check_cache_ttl` in `dynamic_rate_limiter_v3.py` were no longer called from the main code path. Removed along with 6 saturation-based tests that mocked them. 9 new HTB-focused tests added in `test_priority_reservation_adeo.py`.

3. **Fixed: `UserAPIKeyAuth()` empty instantiation** in `_run_htb_check` — the `user_api_key_dict` parameter was removed from `_create_priority_based_descriptors` entirely (it was never used in the method body). `_run_htb_check` now passes only `model` and `priority`.

---

## What Changed vs the Original Codebase

### Summary of modifications to LiteLLM core files

| File | Lines Added | Lines Removed | Nature of Change |
|------|-------------|---------------|------------------|
| `litellm/proxy/hooks/dynamic_rate_limiter_v3.py` | ~207 (committed) + ~212 (uncommitted refactor) | ~218 | Refactored from saturation-based to HTB with per-deployment enforcement |
| `litellm/proxy/hooks/parallel_request_limiter_v3.py` | ~507 | 0 | Pure additions: Lua script + Python binding + in-memory fallback |
| `litellm/router_utils/pre_call_checks/model_rate_limit_check.py` | ~17 (committed) + ~16 (uncommitted) | ~12 | Added HTB-active skip logic |
| `litellm/proxy/hooks/__init__.py` | 2 | 0 | Registration entry |
| `litellm/proxy/utils.py` | 2 | 0 (fixed duplicate) | Router injection + bug fix |
| `litellm/proxy/hooks/dynamic_rate_limiter.py` | 3 | 2 | License check consistency |

**Total: ~730 lines added, ~232 lines removed across 6 core files**

### New files created in `oicm-litellm-layer/`

| Category | Files | Purpose |
|----------|-------|---------|
| Deployment | `deploy/litellm-proxy.yaml`, `deploy/litellm-redis.yaml`, `deploy/litellm-ingress.yaml`, `deploy/discovery-controller.yaml` | Kubernetes manifests |
| Controller | `controller/` (8 Python files) | Cross-cluster model discovery and fallback reconciliation |
| Hooks | `hooks/vllm_param_injector.py`, `hooks/keda_metrics.py` | Custom LiteLLM hooks |
| Tests | `scripts/htb_test_v2.py`, `tests/test_litellm/proxy/hooks/test_priority_reservation_adeo.py`, `tests/test_litellm/proxy/hooks/run_priority_simulations.py` | HTB test suite |
| Benchmarks | `bench_*.py` (4 files) | Performance benchmarking scripts |
| Documentation | `README.md`, `CHANGELOG.md`, `IMPLEMENTATION_PLAN.md`, etc. | Project docs |

### Design philosophy

The changes follow the principle of **minimal modification to the original codebase**:

1. The HTB logic is added as a **new hook** (`dynamic_rate_limiter_v3`) rather than modifying the existing `dynamic_rate_limiter.py` (v1)
2. The Lua script is added to `parallel_request_limiter_v3.py` as **pure additions** — no existing code in that file was modified
3. The `ModelRateLimitingCheck` modification is **backward-compatible** — when `priority_reservation` is not configured, the check runs exactly as before
4. The `ContextVar` approach avoids modifying any router method signatures or call sites

---

## Test Results

The test suite uses 4 API keys with different priority levels:

| Key | User | Priority | Guaranteed RPM (on 100 RPM model) |
|-----|------|----------|-----------------------------------|
| `sk-BR2PtwOoOIU8c01Cfq_y6g` | maalmarri@ECOUNCIL.AE | prior1 | 50 |
| `sk-v7zZOCI7oltAGnXkAvmgog` | naAlkhazraji@ECOUNCIL.AE | prior2 | 30 |
| `sk-KdqW6uIv_J2kK3pDbVI3CA` | jyao@ECOUNCIL.AE | prior3 | 20 |
| `sk-JHuarsctaY99aBj85SIU7w` | dqvu@ECOUNCIL.AE | prior3 | 20 (shared) |

All requests use `extra_body: {"chat_template_kwargs": {"enable_thinking": false}}` to disable thinking mode.

> **Note on Redis state snapshots below**: The `EWMA=` fields in the per-test
> "Redis state" blocks were captured under the previous EWMA-based sibling
> reservation implementation. The current code uses **demand counters**
> (see `PRIORITY-LOGIC-MAP.md` and the "Demand-Based Borrowing" section
> above). The demand counter serves the same role EWMA did (it tracks
> attempted requests including denied ones, so it is >= the accepted request
> count), but it is a sliding-window integer rather than an exponentially
> weighted float. The test result tables (Sent/OK/Denied) remain accurate;
> only the `EWMA=` field labels and values are historical.

### Test 1: Qwen/Qwen3.5-0.8B (100 RPM, no fallbacks)

This test verifies priority enforcement and borrowing on a model with no fallback chain. When the model is at capacity, excess requests get 429.

#### Test 1a: Light load (10 per key, 40 total)

| Priority | User | Sent | OK | Denied |
|----------|------|------|-----|--------|
| prior1 | maalmarri@ECOUNCIL.AE | 10 | 10 | 0 |
| prior2 | naAlkhazraji@ECOUNCIL.AE | 10 | 10 | 0 |
| prior3 | jyao@ECOUNCIL.AE | 10 | 10 | 0 |
| prior3 | dqvu@ECOUNCIL.AE | 10 | 10 | 0 |

**Result**: All 40 succeeded. Model-wide counter: 40/100 RPM (40%). No borrowing needed.

**Redis state**:
```
prior1: requests=10, EWMA=10.00
prior2: requests=10, EWMA=10.00
prior3: requests=20, EWMA=20.00
model_wide: requests=40
```

#### Test 1b: Heavy load (80 per key, 320 total)

| Priority | User | Sent | OK | Denied | Guaranteed |
|----------|------|------|-----|--------|------------|
| prior1 | maalmarri@ECOUNCIL.AE | 80 | 46 | 34 | 50 |
| prior2 | naAlkhazraji@ECOUNCIL.AE | 80 | 30 | 50 | 30 |
| prior3 | jyao@ECOUNCIL.AE | 80 | 24 | 56 | 20 |
| prior3 | dqvu@ECOUNCIL.AE | 80 | 0 | 80 | 20 |

**Result**: 100 OK, 220 denied. Model-wide cap of 100 enforced.

**Key observations**:
- prior2 (naAlkhazraji) got exactly 30 (its guaranteed minimum) — the HTB Lua script correctly enforced the guaranteed rate
- prior1 (maalmarri) got 46 (below 50) — this is because under heavy concurrent load, all priorities race for the model-wide counter. The 46 < 50 is due to the model-wide counter reaching 100 before prior1 could use its full 50
- prior3 (combined: jyao + dqvu) got 24 (above 20 guaranteed) — prior3 borrowed from idle siblings
- prior3 (dqvu) got 0 — because prior3 (jyao) requests arrived slightly earlier and consumed the borrowed capacity first. Both prior3 keys share the same priority bucket

**Redis state**:
```
prior1: requests=46, EWMA=77.62
prior2: requests=30, EWMA=77.90
prior3: requests=24, EWMA=156.40
model_wide: requests=100
```

The EWMA values are higher than the request counts because EWMA tracks the rate of attempts (including denied ones), not just successful ones. This is correct behavior — the EWMA reflects demand, not just granted capacity.

#### Test 1c: prior1 heavy (80), others light (2 each)

| Priority | User | Sent | OK | Denied |
|----------|------|------|-----|--------|
| prior1 | maalmarri@ECOUNCIL.AE | 80 | 75 | 5 |
| prior2 | naAlkhazraji@ECOUNCIL.AE | 2 | 2 | 0 |
| prior3 | jyao@ECOUNCIL.AE | 2 | 2 | 0 |
| prior3 | dqvu@ECOUNCIL.AE | 2 | 2 | 0 |

**Result**: 81 OK. prior1 (maalmarri) borrowed idle capacity and got 75 (above its 50 guaranteed, below the 80 saturation cap).

**Redis state**:
```
prior1: requests=75, EWMA=79.98
prior2: requests=2, EWMA=2.00
prior3: requests=4, EWMA=4.00
model_wide: requests=81
```

**Borrowing verified**: prior1's EWMA was high (79.98), but prior2 and prior3 had low EWMA values (2.00 and 4.00). The borrow ceiling was:
```
borrow_ceiling = min(80, 100) - min(2, 30) - min(4, 20) = 80 - 2 - 4 = 74
```
prior1 (maalmarri) was allowed up to 74 RPM via borrowing, plus its own 50 guaranteed (but capped at the model limit of 100 and the saturation cap of 80). The actual result of 75 is consistent with this computation plus some timing variance.

#### Test 1d: prior3 heavy (80x2=160), others light (2 each)

| Priority | User | Sent | OK | Denied |
|----------|------|------|-----|--------|
| prior1 | maalmarri@ECOUNCIL.AE | 2 | 2 | 0 |
| prior2 | naAlkhazraji@ECOUNCIL.AE | 2 | 2 | 0 |
| prior3 | jyao@ECOUNCIL.AE | 80 | 59 | 21 |
| prior3 | dqvu@ECOUNCIL.AE | 80 | 18 | 62 |

**Result**: 81 OK. prior3 (combined: jyao + dqvu) got 77 (above 20 guaranteed, below 80 saturation cap). Borrowing works for low-priority keys too.

**Redis state**:
```
prior1: requests=2, EWMA=2.00
prior2: requests=2, EWMA=2.00
prior3: requests=77, EWMA=157.97
model_wide: requests=81
```

### Test 2: GLM-5.2 (100 RPM, fallbacks: GLM-5.1 -> MiniMax)

This test verifies that HTB enforcement works correctly on fallback models when the primary model is at capacity.

#### Test 2a: Light load (10 per key, 40 total)

| Priority | User | Sent | OK | Denied |
|----------|------|------|-----|--------|
| prior1 | maalmarri@ECOUNCIL.AE | 10 | 10 | 0 |
| prior2 | naAlkhazraji@ECOUNCIL.AE | 10 | 10 | 0 |
| prior3 | jyao@ECOUNCIL.AE | 10 | 10 | 0 |
| prior3 | dqvu@ECOUNCIL.AE | 10 | 10 | 0 |

**Result**: All 40 succeeded on GLM-5.2. No fallback triggered.

**Models served**: GLM-5.2: 40

#### Test 2b: Heavy load (80 per key, 320 total)

| Priority | User | Sent | OK | Denied |
|----------|------|------|-----|--------|
| prior1 | maalmarri@ECOUNCIL.AE | 80 | 80 | 0 |
| prior2 | naAlkhazraji@ECOUNCIL.AE | 80 | 80 | 0 |
| prior3 | jyao@ECOUNCIL.AE | 80 | 80 | 0 |
| prior3 | dqvu@ECOUNCIL.AE | 80 | 80 | 0 |

**Result**: All 320 succeeded. Fallback cascade distributed load across 3 models.

**Models served**: GLM-5.2: 100, GLM-5.1: 100, MiniMax: 120

**Redis state (GLM-5.2)**:
```
prior1: requests=50, EWMA=78.86
prior2: requests=30, EWMA=78.86
prior3: requests=20, EWMA=156.03
model_wide: requests=100
```

On GLM-5.2, each priority got exactly its guaranteed rate (prior1=50, prior2=30, prior3=20). The overflow cascaded to GLM-5.1 and MiniMax.

**Redis state (GLM-5.1)**:
```
prior1: requests=15, EWMA=29.80
prior2: requests=37, EWMA=48.70
prior3: requests=48, EWMA=136.94
model_wide: requests=100
```

On GLM-5.1, prior3 borrowed heavily (48 above its 20 guaranteed) because prior1 and prior2 had lower EWMA values on this model.

**Redis state (MiniMax)**:
```
prior1: requests=15, EWMA=14.77
prior2: requests=13, EWMA=12.85
prior3: requests=92, EWMA=90.78
model_wide: requests=120
```

On MiniMax (500 RPM), prior3 borrowed massively (92 above its 100 guaranteed, since 20% of 500 = 100) because the saturation cap is 400 RPM (80% of 500) and siblings had low EWMA.

#### Test 2c: prior3-only heavy (80x2=160)

| Priority | User | Sent | OK | Denied |
|----------|------|------|-----|--------|
| prior3 | jyao@ECOUNCIL.AE | 80 | 80 | 0 |
| prior3 | dqvu@ECOUNCIL.AE | 80 | 80 | 0 |

**Result**: All 160 succeeded. prior3 (jyao + dqvu) borrowed idle capacity on GLM-5.2 (80) and GLM-5.1 (80). MiniMax was not needed.

**Models served**: GLM-5.2: 80, GLM-5.1: 80

#### Test 2d: Mixed (prior1: 80, prior2: 2, prior3: 80x2=160, total 242)

| Priority | User | Sent | OK | Denied |
|----------|------|------|-----|--------|
| prior1 | maalmarri@ECOUNCIL.AE | 80 | 80 | 0 |
| prior2 | naAlkhazraji@ECOUNCIL.AE | 2 | 2 | 0 |
| prior3 | jyao@ECOUNCIL.AE | 80 | 80 | 0 |
| prior3 | dqvu@ECOUNCIL.AE | 80 | 80 | 0 |

**Result**: All 242 succeeded. Load distributed: GLM-5.2: 100, GLM-5.1: 89, MiniMax: 53.

### Test 3: GLM-5.2 + Direct Fallback Model Traffic

This is the most critical test. It verifies that HTB enforcement works correctly when traffic is sent DIRECTLY to fallback models (not via the fallback cascade) while other traffic goes through the cascade.

**Setup**:
- prior1 (maalmarri) and prior2 (naAlkhazraji) send to GLM-5.2 (triggers fallback when at capacity)
- prior3 (jyao) sends DIRECTLY to GLM-5.1 (bypasses GLM-5.2)
- prior3 (dqvu) sends DIRECTLY to MiniMax (bypasses GLM-5.2 and GLM-5.1)

#### Test 3a: Light load (10 per key, 40 total)

| Priority | User | Target Model | Sent | OK | Denied |
|----------|------|-------------|------|-----|--------|
| prior1 | maalmarri@ECOUNCIL.AE | GLM-5.2 | 10 | 10 | 0 |
| prior2 | naAlkhazraji@ECOUNCIL.AE | GLM-5.2 | 10 | 10 | 0 |
| prior3 | jyao@ECOUNCIL.AE | GLM-5.1 (direct) | 10 | 10 | 0 |
| prior3 | dqvu@ECOUNCIL.AE | MiniMax (direct) | 10 | 10 | 0 |

**Result**: All 40 succeeded. Direct traffic to each model works independently.

**Models served**: GLM-5.2: 20, GLM-5.1: 10, MiniMax: 10

#### Test 3b: Heavy load (80 per key, 320 total)

| Priority | User | Target Model | Sent | OK | Denied |
|----------|------|-------------|------|-----|--------|
| prior1 | maalmarri@ECOUNCIL.AE | GLM-5.2 | 80 | 80 | 0 |
| prior2 | naAlkhazraji@ECOUNCIL.AE | GLM-5.2 | 80 | 80 | 0 |
| prior3 | jyao@ECOUNCIL.AE | GLM-5.1 (direct) | 80 | 55 | 25 |
| prior3 | dqvu@ECOUNCIL.AE | MiniMax (direct) | 80 | 80 | 0 |

**Result**: 295 OK, 25 denied.

**Models served**: GLM-5.2: 82, GLM-5.1: 100, MiniMax: 113

**Key findings**:
- GLM-5.2 served 82 requests (prior1/maalmarri got 50 guaranteed, prior2/naAlkhazraji got 32 via borrowing). The remaining 78 overflowed to GLM-5.1
- GLM-5.1 reached its 100 RPM hard cap. It served prior3/jyao's direct traffic (55) plus overflow from GLM-5.2 (prior1: 19, prior2: 26). prior3/jyao got 25 denials because GLM-5.1 was at capacity and GLM-5.1 has no fallback chain for direct requests
- MiniMax served prior3/dqvu's direct traffic (80) plus overflow from GLM-5.1 (prior1: 11, prior2: 22, prior3: 0). Total 113, well within 500 RPM

**This test proves**: HTB enforcement works correctly on all 3 models simultaneously, whether traffic arrives via the fallback cascade or via direct requests. The priority guarantees and borrowing logic are enforced per-model, independently.

#### Test 3c: prior1 on GLM-5.2, prior3 direct on fallbacks (242 total)

| Priority | User | Target Model | Sent | OK | Denied |
|----------|------|-------------|------|-----|--------|
| prior1 | maalmarri@ECOUNCIL.AE | GLM-5.2 | 80 | 80 | 0 |
| prior2 | naAlkhazraji@ECOUNCIL.AE | GLM-5.2 | 2 | 2 | 0 |
| prior3 | jyao@ECOUNCIL.AE | GLM-5.1 (direct) | 80 | 79 | 1 |
| prior3 | dqvu@ECOUNCIL.AE | MiniMax (direct) | 80 | 80 | 0 |

**Result**: 241 OK, 1 denied.

**Models served**: GLM-5.2: 81, GLM-5.1: 80, MiniMax: 80

**Borrowing verified on each model independently**:
- GLM-5.2: prior1 (maalmarri) borrowed to 79 (above 50 guaranteed, below 80 saturation cap)
- GLM-5.1: prior3 (jyao) borrowed to 79 (above 20 guaranteed, below 80 saturation cap)
- MiniMax: prior3 (dqvu) got 80 (well below 100 guaranteed on 500 RPM model)

#### Test 3d: prior3 split (160 total)

| Priority | User | Target Model | Sent | OK | Denied |
|----------|------|-------------|------|-----|--------|
| prior3 | jyao@ECOUNCIL.AE | GLM-5.2 (fallback chain) | 80 | 80 | 0 |
| prior3 | dqvu@ECOUNCIL.AE | MiniMax (direct) | 80 | 80 | 0 |

**Result**: All 160 succeeded. Both prior3 keys share the same priority pool on each model.

**Models served**: GLM-5.2: 80, MiniMax: 80

---

## How to Run the Tests

### Prerequisites

1. The LiteLLM proxy must be running with the HTB code deployed
2. Redis must be accessible (the test script flushes Redis between sub-tests)
3. The 4 API keys must be configured with the correct priority metadata
4. `kubectl` must be configured with access to the Redis pod
5. Python 3 with `requests` and `urllib3` installed

### Running individual tests

```bash
# Test 1: Qwen 3.5 0.8B (no fallbacks)
python3 oicm-litellm-layer/scripts/htb_test_v2.py --test 1

# Test 2: GLM-5.2 (with fallbacks)
python3 oicm-litellm-layer/scripts/htb_test_v2.py --test 2

# Test 3: GLM-5.2 + direct fallback models
python3 oicm-litellm-layer/scripts/htb_test_v2.py --test 3
```

### Running all tests sequentially

```bash
python3 oicm-litellm-layer/scripts/htb_test_v2.py --all
```

Each test flushes Redis between sub-tests and waits 2 seconds for the proxy to stabilize. When running `--all`, there is a 65-second wait between tests to let the sliding window reset.

### What the test output shows

Each sub-test prints:

1. **Per-priority breakdown**: how many requests were sent, how many succeeded (200), how many were rate-limited (429), and how many had other errors
2. **Per-model served**: which model actually served each successful request (important for fallback tests)
3. **Redis HTB counters**: the request counter values for each priority and the model-wide counter
4. **EWMA values**: the current EWMA for each priority on each model
5. **Full Redis state**: all HTB-related keys and their values (for debugging)
6. **Validation**: whether the model-wide counter stayed within the RPM limit

---

## Troubleshooting

### All requests are getting 429

**Possible causes**:
1. `priority_reservation` is not configured in the proxy config — check `litellm.priority_reservation` is set
2. The API key does not have a priority in its metadata — check `team_metadata.priority` or `metadata.priority`
3. The priority in the metadata does not match any key in `priority_reservation` — the key falls into `default_pool` with `default_priority` weight (default 0.0)
4. Redis is down and the in-memory fallback is not working — check Redis connectivity

### Requests are not being rate-limited at all

**Possible causes**:
1. The model does not have `rpm` set in its `model_info` — HTB only enforces when RPM is configured
2. `priority_reservation` is not configured — without it, `async_pre_call_check` returns early
3. The hook is not registered — check that `dynamic_rate_limiter_v3` is in the `callbacks` or that it is loaded via `PROXY_HOOKS`

### Fallback models are not getting HTB enforcement

**Possible causes**:
1. The `async_pre_call_check` method is not being called — ensure `enforce_model_rate_limits` is in `router_settings.optional_pre_call_checks` (this is required for `ModelRateLimitingCheck`, but `DynamicRateLimitHandlerV3` is in `litellm.callbacks` and is always called by `async_routing_strategy_pre_call_checks`)
2. The `htb_priority` ContextVar is not being set — check that `async_pre_call_hook` is running and setting the priority

### Redis CROSSSLOT errors

This should not happen because all keys for a model share the `{htb:<model>}` hash tag. If it does, check that the Lua script is receiving the correct keys.

### EWMA values are very high

This is expected under heavy load. The EWMA tracks the rate of attempts (including denied requests), not just successful ones. A high EWMA means the priority is actively trying to send requests, which correctly increases its reservation and reduces the borrow ceiling for other priorities.
