# Feasibility Study: Bridging HTB Priority (Proxy Layer) to Server-Side Priority Preemption (GPU Layer)

> Built with the [Logic Mapping Technique](../logic_mapping_technique.md).
> This document is Phase 1 (Trace) output. Every claim has a verified
> `file:line` reference in the current codebase (branch `jya0-v1.92.0`).

---

## Executive Summary

**Feasible.** The bridge requires one new CustomLogger hook that reads the
`htb_priority` ContextVar and looks up a **config-driven map**
(`priority_body_fields` in `litellm_settings`) to inject field-value pairs
into `data["extra_body"]`. The `extra_body` path bypasses both the router's
integer-priority interception and the `drop_params` stripping, so the fields
reach the upstream vLLM/SGLang server body intact.

The map is a dict-of-dicts supporting many-to-many mappings: multiple HTB
priority strings can map to the same field-value dict (aliases), and each
entry can inject multiple fields for different engines (e.g. `priority` for
SGLang, `urgency` for a future engine). Unknown fields are silently ignored
by OpenAI-compatible servers, so all configured fields can be injected into
every request without per-engine branching.

The bridge is one-directional (proxy priority → server body fields). No
changes to the HTB rate limiter, router, or server deployments are needed.
Adding new priority strings, aliases, or engine-specific fields is a config
edit + restart, not a code change.

---

## The Two Disconnected Priority Systems

### System A: HTB Priority Rate Limiting (Proxy Layer)

Already built and deployed. Controls **how many RPM** each priority class gets.

```
Client sends request with API key (metadata.priority = "prior1")
    |
    v
async_pre_call_hook  (dynamic_rate_limiter_v3.py:382)
    |  extracts "prior1" from user_api_key_dict
    |  sets htb_priority ContextVar = "prior1"  (line 392)
    |  returns None (does NOT modify data dict)
    |
    v
Router picks deployment
    |
    v
async_pre_call_check  (dynamic_rate_limiter_v3.py:395)
    |  reads htb_priority.get() = "prior1"
    |  runs HTB Lua script against Redis  (parallel_request_limiter_v3.py:183)
    |  ALLOW → returns deployment dict
    |  DENY  → raises RateLimitError
    |
    v
Request sent to upstream vLLM/SGLang
    WITHOUT any server-side priority field in the body
```

Key characteristics:
- Priority is a **string** ("prior1", "prior2", "prior3")
- Configured via `priority_reservation` in litellm_settings
  (`litellm-proxy.yaml:40-43`): prior1=0.50, prior2=0.30, prior3=0.20
- Carried by `htb_priority: ContextVar[Optional[str]]`
  (`dynamic_rate_limiter_v3.py:37`)
- Set in `async_pre_call_hook` (line 392), read in `async_pre_call_check`
  (line 399)
- Controls RPM allocation, NOT GPU scheduling
- The hook returns `None` from `async_pre_call_hook` — it does NOT modify the
  request `data` dict at all (line 393)

### System B: Server-Side Priority Preemption (GPU Layer)

Already enabled on GLM deployments. Controls **which requests get KV-cache**
at the GPU level.

```
Client sends request with "priority": 0 in JSON body
    |
    v
vLLM/SGLang receives body with "priority" field
    |
    v
Scheduler orders requests by priority value (lower = higher priority)
    |
    v
P1 (priority=0) request arrives → scheduler preempts low-priority running requests
    |  evicted KV blocks → HiCache CPU offload (preserved, not lost)
    |  P1 request gets KV-cache allocation
    |
    v
P1 request processes → evicted requests resume from CPU-offloaded KV
```

Key characteristics:
- Priority is an **integer** (0=highest/P1, 200=low)
- SGLang flags: `--enable-priority-scheduling`,
  `--priority-scheduling-preemption-threshold 1`,
  `--schedule-low-priority-values-first` (makes 0=highest, OpenAI convention)
- vLLM flag: `--scheduling-policy priority`
- Client sends `"priority"` field directly in request JSON body
- Controls GPU KV-cache preemption, NOT RPM
- Referenced in:
  `devops-custom-models/docs/oicm/hicache/sglang-hicache-priority-preemption-guide.md`
  and
  `devops-custom-models/docs/oicm/hicache/vllm-kv-offload-priority-preemption-guide.md`

### The Gap

A prior1 API key gets RPM guarantee at the proxy, but the request arrives at
vLLM/SGLang **without** a server-side `priority` field, so it gets FCFS
treatment at the GPU level. The two systems never connect.

---

## Phase 1 Trace: Three Obstacles and Their Resolution

### Obstacle 1: The Router's Own Integer-Priority Interception

**The problem.** LiteLLM's router has a **third** priority system: an integer
`priority` kwarg used for latency-based FlowItem queue scheduling. When
present and is an int, the router intercepts it and **pops it from kwargs**
before calling the upstream provider. The integer never reaches the server.

**Trace:**

```
router.py:1835  request_priority = kwargs.get("priority") or self.default_priority
router.py:1841  if request_priority is not None and isinstance(request_priority, int):
router.py:1842      response = await self.schedule_acompletion(**kwargs)
                    |
                    v
router.py:3317  schedule_acompletion(self, ..., priority: int, ..., **kwargs)
router.py:3333  item = FlowItem(priority=priority, ...)
router.py:3376  _response = await self.acompletion(model=model, ..., **kwargs)
                    ^^^ priority was a named param, NOT in **kwargs
                    ^^^ so it's NOT forwarded — it's consumed by the scheduler
```

The same pattern exists for `atext_completion` (`router.py:4045`:
`kwargs.pop("priority")`) and `_schedule_factory` (`router.py:3376`: calls
`original_function(*args, **kwargs)` without priority).

**Resolution.** Do NOT put the server-side priority as a top-level `priority`
key in `data`. The router will intercept it. Instead, put it inside
`data["extra_body"]["priority"]`. The `extra_body` dict is never checked by
`kwargs.get("priority")` because it's a nested dict key, not a top-level
kwarg. The router's `acompletion` signature has no `extra_body` named
parameter, so it flows through as part of `**kwargs` and eventually reaches
the provider handler.

### Obstacle 2: `drop_params: True` Stripping Unknown Params

**The problem.** The production config has `drop_params` behavior. If
`drop_params` strips the `priority` field before it reaches the upstream, the
bridge fails silently.

**Trace:**

```
utils.py:3753  get_optional_params(model=, ..., **kwargs)
               |  passed_params = locals().copy()
               |  extra_body is NOT a named param → stays in **kwargs → special_params
               |
               v
utils.py:3850  _check_valid_arg(supported_params)
               |  iterates non_default_params (top-level named params only)
               |  extra_body is NOT in non_default_params (it's in kwargs)
               |  → priority inside extra_body is NEVER checked by _check_valid_arg
               |
               v
utils.py:4355  add_provider_specific_params_to_optional_params()
utils.py:4366  extra_body = dict(passed_params.pop("extra_body", None) or {})
               |  pops extra_body from passed_params
               |  merges into optional_params["extra_body"]
               |  NO _check_valid_arg applied to extra_body contents
               |  only additional_drop_params list is checked (line 4379)
```

The `drop_params` logic only applies to top-level `non_default_params` (line
3003: `if litellm.drop_params is True ... and k not in supported_params`).
The `extra_body` dict is handled separately at line 4366 — it is popped from
`passed_params` and merged into `optional_params["extra_body"]` WITHOUT being
subject to `_check_valid_arg`. Only `additional_drop_params` (an explicit
deny-list) can remove keys from `extra_body` (line 4379), and `priority` is
not in any deny-list.

**Resolution.** `extra_body` contents are immune to `drop_params`. Injecting
`priority` into `extra_body` is safe.

### Obstacle 3: `extra_body` Must Reach the HTTP Body

**The problem.** Need to confirm `extra_body` actually gets spread into the
final HTTP request body sent to vLLM/SGLang.

**Trace:**

```
openai_like/chat/handler.py:241  extra_body = optional_params.pop("extra_body", {})
openai_like/chat/handler.py:258  data = {
                                    "model": model,
                                    "messages": messages,
                                    **optional_params,
                                    **extra_body,        ← SPREAD INTO BODY
                                  }
                                |
                                v
                                HTTP POST to vLLM/SGLang /v1/chat/completions
                                body = JSON.dumps(data)
                                → "priority" field is in the body
```

The openai_like chat handler does `data = {**optional_params, **extra_body}`
at line 258. This spreads every key from `extra_body` into the top-level
JSON body. The `priority` key becomes a top-level field in the HTTP request
body, which is exactly what vLLM/SGLang expect.

**Resolution.** Confirmed. `extra_body["priority"]` → HTTP body `"priority"`
field. The path is intact.

---

## The Bridge: Injection Point and Data Flow

### Injection Point: `async_pre_call_hook`

The `async_pre_call_hook` runs **before** the router processes the request
(see `proxy/utils.py:1431`). This is where both the existing
`vllm_param_injector` and the HTB hook operate. The bridge hook must run
**after** the HTB hook sets the `htb_priority` ContextVar.

**Hook execution order in the proxy:**

```
proxy_server.py:8427  chat_completion()
    |
    v
proxy/utils.py:1380  _maybe_execute_pipelines (pre_call)
    |
    v
proxy/utils.py:1425  for _callback in caps.resolved_callbacks:
    |     |
    |     v
    |  [callback 1] DynamicRateLimitHandlerV3.async_pre_call_hook
    |     |  sets htb_priority ContextVar = "prior1"
    |     |  returns None
    |     |
    |  [callback 2] VllmParamInjector.async_pre_call_hook
    |     |  relocates vllm-specific params into extra_body
    |     |  returns data (or None)
    |     |
    |  [callback 3] PriorityBridge.async_pre_call_hook  ← BRIDGE
    |     |  reads htb_priority.get() = "prior1"
    |     |  looks up priority_body_fields["prior1"] from litellm_settings
    |     |  → {"priority": 0, "urgency": "high"}
    |     |  merges into data["extra_body"]
    |     |  returns data (modified)
    |     |
    v
router.acompletion(**data)
    |  kwargs.get("priority") → None (priority is in extra_body, not top-level)
    |  → routes to async_function_with_fallbacks (NOT schedule_acompletion)
    |  → no router priority interception
    |
    v
provider handler: get_optional_params
    |  extra_body survives drop_params (Obstacle 2 resolution)
    |
    v
openai_like/chat/handler.py:258
    |  data = {**optional_params, **extra_body}
    |  → "priority": 0, "urgency": "high" in HTTP body
    |
    v
vLLM/SGLang receives body with "priority" and "urgency"
    → reads "priority": 0, ignores unknown "urgency" field
    → P1 preemption enabled
```

---

## End-to-End Verification Trace: Hook Return → SGLang HTTP Body

This section traces every link in the chain from the PriorityBridge hook's
return value to the final HTTP body received by SGLang. Each step has a
verified `file:line` reference. This is the answer to "will the `priority`
field actually reach the SGLang served endpoint?"

### Link 1: Hook return → `process_pre_call_hook_response`

```
proxy/utils.py:1431  response = await _callback.async_pre_call_hook(
                        user_api_key_dict=..., cache=..., data=data, call_type=...)
                    # PriorityBridge returns the modified data dict
                    # (with data["extra_body"]["priority"] = 0)

proxy/utils.py:1438  if response is not None:
                        data = await self.process_pre_call_hook_response(
                            response=response, data=data, call_type=call_type)

proxy/utils.py:906   async def process_pre_call_hook_response(self, response, data, call_type):
proxy/utils.py:910       if isinstance(response, dict):
proxy/utils.py:911           return response      ← RETURNS HOOK'S DICT DIRECTLY
```

**Verified.** When the hook returns a dict, `process_pre_call_hook_response`
returns it as-is (line 911). The modified `data` dict (containing
`extra_body["priority"]`) replaces the original `data`. No keys are lost, no
merging occurs; the hook's dict becomes the request `data`.

### Link 2: `data` → `route_request` → `router.acompletion(**data)`

```
common_request_processing.py:1157  self.data = await proxy_logging_obj.pre_call_hook(...)
                    # self.data now contains extra_body["priority"]

common_request_processing.py:1408  llm_call = await route_request(
                        data=self.data, route_type=route_type,
                        llm_router=llm_router, ...)

route_llm_request.py:390  return getattr(llm_router, f"{route_type}")(**data)
                    # For chat completions: llm_router.acompletion(**data)
                    # data is unpacked as **kwargs
                    # extra_body becomes kwargs["extra_body"] = {"priority": 0, ...}
```

**Verified.** `route_request` unpacks the entire `data` dict as `**kwargs`
into `llm_router.acompletion()`. The `extra_body` key flows through as a
kwarg. No filtering occurs in `route_request` (it only strips
`mock_testing_*` flags at line 363 and pops `router_settings_override`).

### Link 3: `router.acompletion` → no interception → `_acompletion`

```
router.py:1835  request_priority = kwargs.get("priority") or self.default_priority
                # kwargs["priority"] does NOT exist
                # priority is inside kwargs["extra_body"]["priority"], a nested dict
                # kwargs.get("priority") returns None
                # request_priority = self.default_priority (None if not configured)

router.py:1841  if request_priority is not None and isinstance(request_priority, int):
router.py:1842      response = await self.schedule_acompletion(**kwargs)
                # NOT taken — request_priority is None or not an int

router.py:1843  else:
                    response = await self.async_function_with_fallbacks(**kwargs)
                # TAKEN — normal path, extra_body flows through as **kwargs
```

**Verified.** The router only checks `kwargs.get("priority")` (top-level).
Since `priority` is nested inside `extra_body`, this returns `None`. The
request takes the normal `async_function_with_fallbacks` path. `extra_body`
is never touched by the router. (Confirmed: `grep` for `extra_body` in
router.py returns zero matches; the router never reads or pops it.)

### Link 4: `_acompletion` → `litellm.acompletion(**input_kwargs)`

```
router.py:2611  async def _acompletion(self, model, messages, **kwargs):
                    # kwargs contains extra_body = {"priority": 0, ...}

router.py:2685      input_kwargs = {
                        **litellm_params,       # deployment config
                        "messages": messages,
                        "caching": self.cache_responses,
                        "client": model_client,
                        **kwargs,               # extra_body flows through here
                    }

router.py:2690      _response = litellm.acompletion(**input_kwargs)
                    # extra_body passed as kwarg to litellm.acompletion()
```

**Verified.** `_acompletion` merges `**kwargs` (containing `extra_body`) into
`input_kwargs` and passes everything to `litellm.acompletion()`. No keys are
popped except `specific_deployment` (line 2634), `silent_model` (line 2673),
and `include_fallback_errors` (line 2687). `extra_body` is untouched.

### Link 5: `litellm.acompletion` → `get_optional_params`

```
main.py:5208    optional_params = get_optional_params(**optional_param_args, **non_default_params)
                # extra_body is not a named param of acompletion()
                # → it goes into **kwargs → non_default_params
                # → passed to get_optional_params as part of **non_default_params
```

**Verified.** `extra_body` is not a named parameter of `litellm.acompletion`,
so it lands in `**kwargs` which becomes `non_default_params`. These are
forwarded to `get_optional_params`.

### Link 6: `get_optional_params` → `add_provider_specific_params_to_optional_params`

```
utils.py:4363  if custom_llm_provider in ["openai", "azure", "text-completion-openai"]
                   + litellm.openai_compatible_providers:
                   # "openai" is explicitly listed (utils.py:4363)
                   # "hosted_vllm" is in openai_compatible_providers (constants.py:745)
                   # SGLang endpoints registered as openai/ or hosted_vllm/ → BOTH match

utils.py:4366      extra_body = dict(passed_params.pop("extra_body", None) or {})
                   # Pops extra_body from passed_params
                   # extra_body = {"priority": 0, "urgency": "high", ...}

utils.py:4373      initial_extra_body = {
                       **optional_params.get("extra_body", {}),  # existing
                       **extra_body,                            # our injected fields
                   }
                   # Merges our priority fields into extra_body

utils.py:4379      if additional_drop_params is not None:
                       processed_extra_body = {k: v for k, v in initial_extra_body.items()
                                               if k not in additional_drop_params}
                   # Only an explicit additional_drop_params list can strip keys
                   # "priority" is not in any deny-list → survives

utils.py:4383      optional_params["extra_body"] = _ensure_extra_body_is_safe(extra_body=...)
                   # extra_body (with "priority") is stored in optional_params
```

**Verified.** For `openai` and all `openai_compatible_providers` (which
includes `hosted_vllm`), `extra_body` is popped from `passed_params` and
merged into `optional_params["extra_body"]`. The `additional_drop_params`
deny-list is the only filter, and `priority` is not in any configured
deny-list. The `drop_params` / `_check_valid_arg` logic (which only applies
to top-level `non_default_params`) never touches `extra_body` contents.

### Link 7: `openai_like` handler → HTTP body

```
handler.py:241  extra_body = optional_params.pop("extra_body", {})
                # Pops extra_body = {"priority": 0, "urgency": "high", ...}

handler.py:258  data = {
                    "model": model,
                    "messages": messages,
                    **optional_params,     # standard params (temperature, etc.)
                    **extra_body,          # SPREADS priority INTO TOP-LEVEL BODY
                }
                # data = {"model": ..., "messages": ..., "priority": 0, "urgency": "high", ...}

handler.py:266  logging_obj.pre_call(... additional_args={"complete_input_dict": data, ...})
                # data is serialized to JSON and sent as HTTP POST body
```

**Verified.** The `openai_like` chat handler spreads every key from
`extra_body` into the top-level `data` dict at line 258. The `priority` key
becomes a top-level field in the JSON body sent to SGLang's
`/v1/chat/completions` endpoint.

### Link 8: SGLang receives the body

SGLang's OpenAI-compatible `/v1/chat/completions` endpoint parses the JSON
body. With `--enable-priority-scheduling` and
`--schedule-low-priority-values-first`, SGLang reads the `priority` field
and uses it for KV-cache preemption scheduling. Unknown fields (e.g.
`urgency`) are silently ignored by the Pydantic schema parser.

### Complete Chain Summary

```
PriorityBridge.async_pre_call_hook
  returns data with data["extra_body"]["priority"] = 0
  (proxy/utils.py:1431)
        │
        ▼
process_pre_call_hook_response
  returns hook's dict directly (isinstance dict → return response)
  (proxy/utils.py:906-911)
        │
        ▼
base_process_llm_request
  calls route_request(data=self.data, ...)
  (common_request_processing.py:1408)
        │
        ▼
route_request
  calls llm_router.acompletion(**data)
  (route_llm_request.py:390)
  extra_body becomes kwargs["extra_body"]
        │
        ▼
router.acompletion
  kwargs.get("priority") → None (priority is in extra_body, not top-level)
  routes to async_function_with_fallbacks (NOT schedule_acompletion)
  (router.py:1835-1843)
        │
        ▼
router._acompletion
  builds input_kwargs = {**litellm_params, **kwargs}
  calls litellm.acompletion(**input_kwargs)
  (router.py:2685-2690)
        │
        ▼
litellm.acompletion
  extra_body goes into non_default_params → **kwargs
  calls get_optional_params(**optional_param_args, **non_default_params)
  (main.py:5208)
        │
        ▼
get_optional_params → add_provider_specific_params_to_optional_params
  custom_llm_provider in ["openai", ...] + openai_compatible_providers → True
  pops extra_body from passed_params
  merges into optional_params["extra_body"]
  no additional_drop_params strips "priority"
  (utils.py:4363-4383)
        │
        ▼
openai_like/chat/handler
  pops extra_body from optional_params
  data = {**optional_params, **extra_body}
  "priority" becomes top-level key in HTTP body
  (handler.py:241, 258)
        │
        ▼
HTTP POST to SGLang /v1/chat/completions
  body contains {"model": ..., "messages": ..., "priority": 0, ...}
  SGLang reads "priority": 0 → P1 preemption enabled
```

**Answer: Yes.** The `priority` field injected by the PriorityBridge hook
into `data["extra_body"]` will reach the SGLang served endpoint as a
top-level field in the HTTP request body. Every link in the chain is
verified with file:line references. No intermediate step pops, strips, or
intercepts `extra_body` contents. The only residual risk is hook execution
ordering (the HTB hook must set `htb_priority` before the bridge hook reads
it), which is a Phase 2 test concern, not a code-path concern.

### Priority Mapping: Config-Driven, Many-to-Many

The map is **not hardcoded** in the hook. It lives in `litellm_settings` in
the config YAML as `priority_body_fields`, alongside the existing
`priority_reservation`. The hook reads it at startup.

The map is a **dict of dicts**: each HTB priority string maps to a dict of
field-value pairs that get injected into `extra_body`. This is inherently
many-to-many:

- **Many string keys → same fields**: multiple HTB priority strings (aliases,
  team names, service tiers) can map to the same field-value dict
- **One string key → many fields**: each entry can inject multiple fields
  (e.g. `priority` for SGLang, `urgency` for a future engine)

OpenAI-compatible servers parse request bodies with Pydantic models that
ignore unknown fields by default. So injecting `"priority": 0` into a
request to an engine that does not support priority scheduling is harmless;
the field is silently dropped by the server's schema parser. This means all
known priority-related fields can be injected into every request, and each
engine picks up only the one it recognizes.

**Config YAML:**

```yaml
litellm_settings:
  priority_reservation:          # RPM enforcement (HTB system)
    prior1: 0.50
    prior2: 0.30
    prior3: 0.20
  priority_body_fields:          # Server body injection (bridge)
    prior1:
      priority: 0                # SGLang/vLLM integer priority
      urgency: high              # Future engine field
    prior2:
      priority: 100
      urgency: medium
    prior3:
      priority: 200
      urgency: low
```

**Current mapping for GLM deployments** (SGLang with
`--schedule-low-priority-values-first`, so 0 = highest):

| HTB Priority (string) | Reservation | Server Fields | Meaning |
|---|---|---|---|
| `prior1` | 50% | `priority=0, urgency=high` | Highest — preempts others |
| `prior2` | 30% | `priority=100, urgency=medium` | Medium |
| `prior3` | 20% | `priority=200, urgency=low` | Lowest — gets preempted |
| (none / no key) | — | (omit all fields) | FCFS (server default) |

**Future extensibility examples** (config-only changes, no code edit):

Add a string alias that maps to the same fields as prior1:
```yaml
  priority_body_fields:
    prior1:
      priority: 0
      urgency: high
    urgent:               # New alias, same behavior as prior1
      priority: 0
      urgency: high
```

Add a field for a new engine without touching existing entries:
```yaml
  priority_body_fields:
    prior1:
      priority: 0
      urgency: high
      scheduling_weight: 1.0   # New engine field
    prior2:
      priority: 100
      urgency: medium
      scheduling_weight: 0.5
```

The lookup is always a single `dict.get()` on the `htb_priority` string. No
per-engine branching, no conditional logic in the hook.

### Data Contract

**Input to bridge hook:**
```python
data: dict = {
    "model": "hosted_vllm/zai-org/GLM-5.2-FP8",
    "messages": [...],
    "metadata": {...},
    # NO "priority" key at top level (router would intercept it)
    # NO "extra_body" key yet (or may exist from vllm_param_injector)
}
htb_priority: ContextVar = "prior1"  # set by HTB hook earlier in the loop
```

**Output from bridge hook:**
```python
data: dict = {
    "model": "hosted_vllm/zai-org/GLM-5.2-FP8",
    "messages": [...],
    "metadata": {...},
    "extra_body": {
        # existing vllm params (if vllm_param_injector ran first)
        "guided_json": {...},
        # NEW: server-side fields injected by bridge (from priority_body_fields config)
        "priority": 0,
        "urgency": "high",
    }
}
```

---

## Implementation: Config-Driven Priority Bridge

### Design Principles

1. **Map is config-driven, not hardcoded.** The mapping lives in
   `litellm_settings.priority_body_fields` in the config YAML. Adding,
   removing, or aliasing priority strings is a config edit + restart, not a
   code change and redeploy.

2. **Many-to-many by structure.** Each HTB priority string maps to a dict of
   field-value pairs. Multiple strings can map to the same dict (aliases).
   Each dict can contain multiple fields (for different engines). No
   per-engine branching in the hook code.

3. **Unknown fields are harmless.** OpenAI-compatible servers ignore
   unrecognized body fields. Injecting `"priority": 0` to an engine without
   priority scheduling support is silently dropped. All configured fields
   are injected into every request; each engine picks up what it recognizes.

4. **Separation of concerns.** `priority_reservation` controls RPM
   enforcement (HTB system). `priority_body_fields` controls what reaches
   the server body (bridge). They are independent concerns that share the
   same string keys.

### Config Schema

```yaml
litellm_settings:
  priority_reservation:          # existing — RPM enforcement
    prior1: 0.50
    prior2: 0.30
    prior3: 0.20
  priority_body_fields:          # new — server body injection
    prior1:
      priority: 0
      urgency: high
    prior2:
      priority: 100
      urgency: medium
    prior3:
      priority: 200
      urgency: low
```

### Hook Logic (sketch — not final code)

The hook reads `priority_body_fields` from litellm settings at startup. On
 each request, it reads `htb_priority.get()`, looks up the field-value dict,
 and merges it into `data["extra_body"]`.

```python
class PriorityBridge(CustomLogger):

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        from litellm.proxy.hooks.dynamic_rate_limiter_v3 import htb_priority

        htb_prio = htb_priority.get()
        body_fields = self._priority_body_fields.get(htb_prio)
        if body_fields is None:
            return None

        extra_body = data.get("extra_body") or {}
        extra_body.update(body_fields)
        data["extra_body"] = extra_body
        return data
```

The entire bridge is one `dict.get()` + one `dict.update()`. The
`_priority_body_fields` dict is populated from `litellm_settings` at
startup (same pattern as `priority_reservation` is already read by the
HTB hook).

### Deployment Variants

The hook exists in two contexts:

1. **Workspace version** (`oicm-litellm-layer/hooks/`): Full Python module,
   testable, reads config from `litellm_settings`.

2. **In-cluster ConfigMap version** (`litellm-proxy.yaml`): Inline Python in
   a ConfigMap, deployed to Kubernetes. Must also read
   `priority_body_fields` from litellm settings. The existing in-cluster
   `vllm_param_injector` already reads `model_info` from the router; the
   priority bridge would read from `litellm_settings` instead, which is
   available at module load time.

Both variants use the same logic; the ConfigMap version is just packaged
differently for the Kubernetes deployment.

### Why Not Extend `vllm_param_injector`

The existing `vllm_param_injector` hook relocates vLLM-specific params
(guided_json, thinking_token_budget, etc.) from `data` into `data["extra_body"]`.
It could be extended to also inject priority fields. However:

- **Coupling**: Priority bridging is a separate concern from vLLM param
  relocation. Mixing them makes the hook harder to reason about.
- **Testability**: A dedicated `PriorityBridge` hook can be unit-tested in
  isolation with a mock ContextVar and a mock config dict.
- **Independence**: The bridge can be enabled/disabled independently by
  adding or removing it from the `callbacks` list, without affecting vLLM
  param injection.
- **Registration order**: The bridge must run after the HTB hook sets
  `htb_priority`. A separate callback in the `callbacks` list makes this
  ordering explicit and controllable.

The tradeoff is one additional callback in the loop, but the clarity and
testability outweigh the marginal overhead of one extra `async_pre_call_hook`
call per request.

---

## Hook Execution Order (Critical)

The bridge hook MUST run after the HTB hook sets `htb_priority`. In LiteLLM,
`async_pre_call_hook` callbacks execute in registration order
(`proxy/utils.py:1425`: `for _callback in caps.resolved_callbacks`).

Current registration in `litellm-proxy.yaml:37-39`:
```yaml
callbacks:
  - litellm_hooks.vllm_param_injector.vllm_param_injector
  - prometheus
```

After adding the bridge:
```yaml
callbacks:
  - litellm_hooks.vllm_param_injector.vllm_param_injector
  - litellm_hooks.priority_bridge.priority_bridge    # NEW
  - prometheus
```

The HTB hook (`DynamicRateLimitHandlerV3`) is registered implicitly via
`priority_reservation` config, not in the `callbacks` list. It runs as part
of the pre-call check flow. The `async_pre_call_hook` for HTB is called in
the same callback loop as explicit callbacks, but its position depends on
whether it's in `litellm.callbacks` or `litellm._priority_reservation_callbacks`.

The `PriorityBridge` hook must appear in the `callbacks` list after
`vllm_param_injector` (so both can contribute to `extra_body` without
conflict) and must run after the HTB hook sets `htb_priority`.

**Verification needed (Phase 2):** Confirm that `htb_priority` is set before
the bridge hook reads it. If the HTB hook runs after the bridge hook in the
loop, the ContextVar will be `None` and no priority will be injected. This
can be tested by logging `htb_priority.get()` inside the bridge hook on a
live request.

---

## What the Bridge Does NOT Do

- **Does not change HTB rate limiting.** The RPM enforcement at the proxy
  layer continues exactly as before. The bridge only adds a server-side
  priority field to the request body.
- **Does not change router scheduling.** Because priority is in `extra_body`
  (not top-level `data["priority"]`), the router's FlowItem scheduler is not
  triggered. The request goes through `async_function_with_fallbacks`, not
  `schedule_acompletion`.
- **Does not change server deployment flags.** The SGLang/vLLM flags
  (`--enable-priority-scheduling`, etc.) are already enabled on the GLM
  deployments.
- **Does not handle bidirectional feedback.** If the server rejects a request
  due to preemption, the proxy does not retry with a different priority. The
  bridge is one-way: proxy priority → server priority.

---

## Phase 2 Test Plan (Not Yet Executed)

Following the logic mapping technique, Phase 2 requires testing against real
scraped data. The following tests are needed:

### Test 1: Verify `htb_priority` ContextVar is set before bridge hook

Scrape: Send a request with a prior1 API key to the live proxy. Add temporary
logging in the bridge hook to print `htb_priority.get()`.

Expected: `"prior1"` (not `None`).

### Test 2: Verify `extra_body["priority"]` reaches upstream

Scrape: Port-forward to the vLLM/SGLang pod. Send a request through the proxy
with a prior1 API key. Capture the request body at the server (via
`--detailed-debug` or a tcpdump).

Expected: The request body contains `"priority": 0`.

### Test 3: Verify no router priority interception

Scrape: Send a request with `"priority": 0` directly in the body (not via
API key). Check whether the router's `schedule_acompletion` is invoked (look
for `x-litellm-request-prioritization-used` header in the response).

Expected: When priority is in `extra_body`, the header is absent (router did
not intercept). When priority is top-level, the header is present (router
intercepted).

### Test 4: Verify server-side preemption triggers

Scrape: Send concurrent requests: prior1 (priority=0) + prior3 (priority=200).
Monitor vLLM/SGLang scheduler metrics for preemption events.

Expected: prior1 requests preempt prior3 running requests (KV-cache eviction
count > 0).

---

## Risks and Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Hook execution order: HTB hook runs after bridge hook | Medium | High (no priority injected) | Test 1 verifies; can force order via callback registration |
| `extra_body` is `None` (not `{}`) | Low | Low (TypeError) | Guard with `data.get("extra_body") or {}` |
| Server doesn't recognize `priority` field | Low | Low (ignored) | Already verified servers have `--enable-priority-scheduling` |
| Router intercepts priority from `extra_body` | Very Low | High (priority consumed) | Trace confirms router only checks `kwargs.get("priority")`, not nested dicts |
| `additional_drop_params` list includes "priority" | Very Low | Medium (field stripped) | Check config for `additional_drop_params`; none currently configured |
| Non-hosted_vllm models get priority injected | Medium | Low (server ignores) | Gate on `model.startswith("hosted_vllm/")` or check model_info |

---

## Conclusion

The bridge is **feasible with minimal code**. The `extra_body` injection path
is verified end-to-end across all 8 links from hook return to SGLang HTTP
body (see "End-to-End Verification Trace" section above):

1. `htb_priority` ContextVar is set by the HTB hook at
   `dynamic_rate_limiter_v3.py:392`
2. A dedicated `PriorityBridge` hook reads it in `async_pre_call_hook` and
   looks up `priority_body_fields` from `litellm_settings` (config-driven,
   not hardcoded)
3. The field-value dict (e.g. `{"priority": 0, "urgency": "high"}`) is
   merged into `data["extra_body"]` and the hook returns the modified `data`
4. `process_pre_call_hook_response` returns the hook's dict directly
   (proxy/utils.py:911) — no keys lost
5. `route_request` unpacks `data` as `**kwargs` into `router.acompletion`
   (route_llm_request.py:390) — `extra_body` flows through as a kwarg
6. `router.acompletion` checks `kwargs.get("priority")` (top-level only,
   router.py:1835) → returns `None` because priority is nested in
   `extra_body` → takes normal path, no interception
7. `router._acompletion` forwards `**kwargs` (including `extra_body`) to
   `litellm.acompletion` (router.py:2690)
8. `litellm.acompletion` passes `extra_body` into `get_optional_params`
   (main.py:5208)
9. `add_provider_specific_params_to_optional_params` merges `extra_body`
   into `optional_params["extra_body"]` for all `openai` and
   `openai_compatible_providers` (utils.py:4363-4383) — no `drop_params`
   stripping applies
10. `openai_like/chat/handler.py:258` spreads `extra_body` into the HTTP
    body: `data = {**optional_params, **extra_body}` — `"priority"` becomes
    a top-level field
11. SGLang receives `"priority": 0` in the body and applies preemption;
    unknown fields like `"urgency"` are silently ignored

**Answer to the verification question: Yes.** The `priority` field will
reach the SGLang served endpoint. Every link in the chain is verified with
file:line references. No intermediate step pops, strips, or intercepts
`extra_body` contents. The router never reads `extra_body` (zero grep hits
in router.py). The `drop_params` / `_check_valid_arg` logic only applies to
top-level named params, not nested `extra_body` keys. The
`additional_drop_params` deny-list is the only filter that could strip
`extra_body` keys, and `priority` is not in any configured deny-list.

The recommended implementation is a **dedicated `PriorityBridge` hook** with
a **config-driven `priority_body_fields` map** in `litellm_settings`. This
approach:

- Requires no code changes to add/alias priority strings or support new
  engines (config edit + restart only)
- Handles many-to-many mappings naturally (multiple strings to same fields,
  one string to multiple fields)
- Keeps priority bridging decoupled from vLLM param injection
- Is independently testable and toggleable

The only residual risk is **hook execution ordering**: the HTB hook must set
`htb_priority` before the bridge hook reads it. This is a runtime concern
(callback registration order), not a code-path concern. Phase 2 (Test)
should verify this on a live system before Phase 3 (Build).
