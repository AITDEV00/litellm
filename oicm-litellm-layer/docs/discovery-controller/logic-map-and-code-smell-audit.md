# Discovery Controller: Logic Map & Code Smell Audit

> **Technique**: [Logic Mapping](../logic_mapping_technique.md) + [Code Smell Detection](../code_smell_detection_technique.md)
>
> **Scope**: `oicm-litellm-layer/controller/` (all 19 Python files across 5 packages)
>
> **Date**: 2025-01

---

## Part 1: Logic Map

### 1.1 Package Structure

```
controller/
├── __main__.py          # Entry point (CLI: --once | daemon)
├── __init__.py          # Empty
├── config.py            # Env-var configuration (single source of truth)
├── controller.py        # DiscoveryController: orchestrates sources, reconciler, litellm
├── models.py            # OicmModel dataclass + detection functions
├── reconciler.py        # SyncReconciler: computes + executes sync plan
├── litellm_client.py    # LiteLLMClient: HTTP client for proxy management API
├── sources/
│   ├── __init__.py      # Exports ModelSource
│   ├── base.py          # ModelSource ABC
│   ├── local_deployments.py    # LocalDeploymentSource (k8s deployments)
│   └── submariner_imports.py  # SubmarinerImportSource (cross-cluster)
├── fallbacks/
│   ├── __init__.py      # Exports FallbackReconciler
│   ├── client.py        # FallbackClient (HTTP)
│   └── service.py       # FallbackReconciler (log-only)
└── pricing/
    ├── __init__.py      # Exports PricingResolver, PricingSource, etc.
    ├── models.py        # PricingEntry, MatcherCandidate, PricingResult (frozen)
    ├── resolver.py      # PricingResolver: orchestrates matchers
    ├── source.py        # PricingSource: loads JSON, builds PricingIndex
    ├── matchers.py      # exact, structured, fuzzy, substring matchers
    ├── normalizer.py    # Model name normalization
    ├── aggregator.py    # Weighted aggregation of candidates
    └── utils.py         # pricing_to_params converter
```

### 1.2 Entry Points

```
__main__.py:29  if "--once" in sys.argv → run_once()
__main__.py:31  else                   → run()

run_once() [__main__.py:14]
    → DiscoveryController()
    → asyncio.run(controller.full_sync())

run() [__main__.py:17]
    → DiscoveryController()
    → signal handlers (SIGINT, SIGTERM → controller.stop())
    → loop.run_until_complete(controller.start())
```

### 1.3 Startup Flow (controller.start)

```
DiscoveryController.start() [controller.py:72]
    │
    ├── sets _running = True
    ├── creates aiohttp web app with /health endpoint
    ├── starts health server on HEALTH_PORT (default 8090)
    │
    ├── await full_sync()  ← initial reconciliation
    │
    └── await asyncio.gather(_watch_loop(), _periodic_resync())
            │                        │
            │                        └── every SYNC_INTERVAL (300s): full_sync()
            │
            └── _watch_once() in a loop
                    │
                    ├── kubernetes.watch.Watch on deployments
                    │   (label: oip/workload-type=model_deployment, ns=adeo)
                    │
                    └── for each event:
                        ├── ADDED   → _handle_add(uuid, dep)
                        ├── DELETED → _handle_delete(uuid)
                        └── MODIFIED→ _handle_modify(uuid, dep)
```

### 1.4 Full Sync Flow (the reconciliation cycle)

```
full_sync() [controller.py:84]
    │
    ├── 1. DISCOVER: for each source in self.sources:
    │   │
    │   ├── LocalDeploymentSource.discover() [local_deployments.py:42]
    │   │   │
    │   │   ├── list_namespaced_deployment(ns=adeo, label=model_deployment)
    │   │   │
    │   │   └── for each deployment:
    │   │       ├── uuid = labels[oip/workload-id]
    │   │       ├── _discover_model_id(uuid)
    │   │       │   ├── _get_configmap_field(uuid, "MODEL_ID")  ← k8s API
    │   │       │   └── if empty: _query_v1_models(uuid)         ← HTTP /v1/models
    │   │       ├── _get_configmap_field(uuid, "EXTRA_ARGS")     ← k8s API
    │   │       ├── _probe_openapi_paths(uuid)                   ← HTTP /openapi.json
    │   │       ├── _discover_owned_by(uuid)                     ← HTTP /v1/models
    │   │       ├── detect_mode_from_paths(paths, model_id, extra_args)
    │   │       ├── detect_provider(owned_by, model_id)
    │   │       └── build OicmModel(uuid, model_id, mode, provider, ...)
    │   │
    │   └── SubmarinerImportSource.discover() [submariner_imports.py:43]
    │       ├── list EndpointSlices (label: lighthouse + model_deployment)
    │       └── for each slice:
    │           ├── _query_v1_models(globalnet_ip, port)  ← HTTP /v1/models
    │           ├── detect_mode(model_id, "")  ← DEGRADED (no OpenAPI probe)
    │           └── build OicmModel(provider defaults to "hosted_vllm")
    │
    ├── 2. LIST LITELLM: litellm.list_all_models_by_uuid()
    │   └── GET /model/info → group by oicm_uuid
    │
    ├── 3. COMPUTE PLAN: reconciler.compute_plan(discovered, litellm_by_uuid)
    │   │
    │   ├── litellm-only uuids → plan.deletes (stale models to remove)
    │   ├── k8s-only uuids     → plan.registers (new models to add)
    │   └── both uuids:
    │       ├── name changed → delete old + register new
    │       └── name same    → patch (model, api_base, pricing)
    │
    ├── 4. EXECUTE PLAN: reconciler.execute(plan)
    │   └── litellm.batch(deletes, registers, patches)  ← concurrent HTTP
    │
    ├── 5. UPDATE STATE: _state = plan.new_state, _litellm_id_map = plan.new_id_map
    │
    └── 6. FALLBACKS: fallback_reconciler.reconcile()  ← log-only, no-op
```

### 1.5 Event-Driven Flow (watch loop)

```
_handle_add(uuid, dep) [controller.py:170]
    │
    ├── skip if uuid already in _state
    ├── skip if ready_replicas == 0
    │
    ├── discover_model_id(uuid)       ← duplicate of discover() logic
    ├── get_configmap_field(uuid, "EXTRA_ARGS")
    ├── probe_openapi_paths(uuid)
    ├── discover_owned_by(uuid)
    ├── detect_mode_from_paths(...)
    ├── detect_provider(...)
    │
    ├── build OicmModel
    ├── pricing_resolver.resolve(model_id)
    ├── litellm.register_model(model, inherited)  ← direct register, bypasses reconciler
    │
    └── update _litellm_id_map + _state

_handle_delete(uuid) [controller.py:215]
    ├── litellm.deregister_model(litellm_id)  ← direct delete
    ├── pop _state[uuid]
    └── fallback_reconciler.reconcile()

_handle_modify(uuid, dep) [controller.py:222]
    ├── if old_model exists: MUTATE ready_replicas + total_replicas
    │   (no litellm API call, no mode/provider re-detection)
    └── if no old_model and ready > 0: _handle_add(uuid, dep)
```

### 1.6 Pricing Subsystem Flow

```
PricingResolver.resolve(model_id) [resolver.py:28]
    │
    ├── if not PRICING_ENABLED → return None
    ├── source.get_index() → PricingIndex (cached, refreshed every 300s)
    │   ├── _load_from_file(PRICING_JSON_PATH)  ← local JSON file
    │   └── fallback: _load_from_proxy(base_url)  ← GET /model/info
    │
    ├── normalize_model_name(model_id) [normalizer.py:16]
    │   ├── strip org prefix (rsplit "/")
    │   ├── strip suffixes (-instruct, -turbo, -fp8, dates, etc.)
    │   ├── lower + collapse dashes
    │   └── return normalized string
    │
    ├── run DEFAULT_MATCHERS (exact → structured → fuzzy → substring)
    │   each returns tuple[MatcherCandidate, ...]
    │
    └── aggregate(candidates) [aggregator.py:13]
        ├── if exact match (score=1.0) → return that candidate
        ├── filter by threshold (PRICING_MATCH_THRESHOLD)
        ├── if 1 candidate → return it
        └── if multiple → weighted average by score
```

### 1.7 Data Contracts

```
OicmModel [models.py:8]
    uuid: str                    # k8s workload-id or "submariner:cluster:uuid"
    model_id: str                # from ConfigMap, /v1/models, or UUID fallback
    model_name: str              # sanitize_model_id(model_id)
    namespace: str               # "adeo"
    ready_replicas: int          # from deployment status
    total_replicas: int          # from deployment status
    mode: str = "chat"           # detect_mode_from_paths result
    provider: str = "hosted_vllm"# detect_provider result
    litellm_model_id: str|None   # set after registration
    extra_args: str = ""         # from ConfigMap EXTRA_ARGS
    source: str = "local"        # "local" or "submariner:cluster"
    api_base_override: str|None  # set for submariner imports

    @property api_base → http://s-{uuid}.{ns}.{domain}:{port}/v1
    @property is_ready → ready_replicas > 0

SyncPlan [reconciler.py:30]
    deletes:    list[str]                    # litellm model IDs to delete
    registers:  list[(OicmModel, dict|None)] # models to register + pricing params
    patches:    list[(str, dict)]            # litellm ID + params to patch
    new_state:  dict[str, OicmModel]         # post-sync state
    new_id_map: dict[str, str]              # uuid → litellm model ID

LiteLLM register payload [litellm_client.py:139]
    model_name: str
    litellm_params: {model, api_base, api_key, drop_params, ...pricing}
    model_info: {mode, oicm_uuid, oicm_namespace, oicm_source}
```

---

## Part 2: Code Smell Audit

### L1: Automated Tool Baseline

#### Initial Baseline (before fixes)

| Tool | Finding | File | Severity |
|---|---|---|---|
| pyflakes | `field` imported but unused | models.py:1 | Fix |
| pyflakes | `asyncio` imported but unused | health.py:3 | Fix (file deleted) |
| pyflakes | `_config` imported but unused | __main__.py:6 | Intentional (side-effect) |
| pyflakes | `existing_params` assigned but unused | reconciler.py:94 | Fix |
| pyflakes | `Optional` imported but unused | fallbacks/client.py:2 | Fix |
| vulture | unused variable `frame` | __main__.py:21 | Fix |
| vulture | unused variable `request` | controller.py:93 | Fix |
| vulture | unused variable `request` | health.py:10 | Fix (file deleted) |
| ruff ARG001 | unused arg `frame` | __main__.py:21 | Fix |
| ruff ARG002 | unused arg `request` | controller.py:93 | Fix |
| ruff F401 | `Optional` unused | fallbacks/client.py:2 | Fix |
| ruff F401 | `asyncio` unused | health.py:3 | Fix (file deleted) |
| ruff F401 | `field` unused | models.py:1 | Fix |
| ruff ARG001 | unused arg `request` | health.py:10 | Fix (file deleted) |
| ruff F841 | `existing_params` unused | reconciler.py:94 | Fix |
| ruff PLR0911 | too many returns (7>6) | models.py:50 | Suppress (correct pattern) |
| ruff PLR2004 | magic value 0.80 | matchers.py:100 | Ignore (threshold constant) |
| ruff PLR2004 | magic value 405, 200 | local_deployments.py, submariner_imports.py | Ignore (HTTP status codes) |

#### After Fixes (L4 recheck)

| Tool | Result |
|---|---|
| pyflakes | 1 finding: `_config` (intentional side-effect import, `# noqa: F401`) |
| vulture | Clean |
| ruff | All checks passed |

### L2: Per-File Semantic Checklist

#### controller/models.py

- **[FIXED]** `field` imported but unused (L1)
- **[OK]** `OicmModel` is a mutable dataclass (not frozen). Violates immutability principle but is actively mutated by `_handle_modify`. Design decision, not a fix for this pass
- **[OK]** `detect_mode` is a backward-compat wrapper passing `frozenset()` to `detect_mode_from_paths`. Used only by submariner source which doesn't probe OpenAPI. Functional but degrades detection quality
- **[OK]** `detect_provider` uses tuple of known providers. Adding a new provider requires code change. Acceptable for current scale

#### controller/controller.py

- **[FIXED]** `_health(self, request)` → `_health(self, _request)` (L1)
- **[SMELL: MEDIUM]** `_handle_add` (lines 170-213) duplicates discovery logic from `LocalDeploymentSource.discover()` (lines 42-79). Two parallel code paths that can diverge. The event-driven path calls `litellm.register_model` directly, bypassing the reconciler, while the full_sync path goes through `compute_plan` → `execute`. If discovery logic changes in one place but not the other, models registered via events will have different mode/provider detection than models registered via full_sync
- **[SMELL: MEDIUM]** `_handle_modify` (lines 222-231) mutates `old_model.ready_replicas` and `old_model.total_replicas` directly instead of creating a new `OicmModel`. If a deployment's image changes (model_id, mode, or provider change), the modify handler never re-probes or re-registers. It silently keeps stale mode/provider
- **[SMELL: LOW]** Inline health server in `start()` (lines 74-81) duplicates the deleted `health.py` module. Now consolidated, but the inline approach means health server lifecycle is coupled to controller lifecycle

#### controller/reconciler.py

- **[FIXED]** `existing_params` unused variable removed (L1)
- **[SMELL: MEDIUM]** `SyncPlan` uses mutable `field(default_factory=...)` for lists and dicts. Violates immutability principle. The plan is built incrementally in `compute_plan` then mutated in `execute` (new_id_map, new_state updated). A frozen dataclass with builder pattern would be cleaner but would require significant restructuring
- **[SMELL: LOW]** `CONFIG_KEYS` (line 16) is a module-level set used only in `_pick_richest_entry`. Could be a function-local constant, but module-level is acceptable for readability
- **[SMELL: LOW]** `compute_plan` has a gap: when names match but `existing_id` is None (no model_id in litellm entry), the model is neither patched nor registered. It's silently dropped from `new_state`. The full_sync will catch it next cycle as a k8s-only uuid, but there's a one-cycle gap

#### controller/litellm_client.py

- **[FIXED]** `_empty_list()` module-level workaround removed; replaced with `asyncio.gather()` (empty generator yields empty list)
- **[FIXED]** Dead `patch_model` method removed (never called)
- **[FIXED]** `list_all_models_by_uuid` return type: `dict` → `dict[str, list[dict]]`
- **[SMELL: MEDIUM]** Creates a new `httpx.AsyncClient` per `batch()` call and per `list_all_models_by_uuid` call. For the sync interval (300s) this is acceptable, but under event-driven bursts (rapid deployment additions), it creates unnecessary TCP connections. A shared client with connection pooling would be better
- **[SMELL: LOW]** `list_all_models_by_uuid` catches `Exception` and returns `{}`. This silently swallows errors; full_sync will then see no litellm models and try to re-register everything. The error is logged but the behavior is destructive (duplicate registrations)

#### controller/sources/local_deployments.py

- **[SMELL: HIGH]** Duplicate HTTP calls to `/v1/models`. During `discover()`, for each pod:
  1. `_discover_model_id` → `_get_configmap_field` (k8s API) → if empty: `_query_v1_models` (HTTP /v1/models)
  2. `_probe_openapi_paths` (HTTP /openapi.json)
  3. `_discover_owned_by` (HTTP /v1/models)

  If MODEL_ID is not in the ConfigMap, that's 2 calls to `/v1/models` for the same pod. The `_query_v1_models` and `_discover_owned_by` methods both fetch the same endpoint but extract different fields (`id` vs `owned_by`). They should be merged into a single call that returns both

- **[SMELL: LOW]** Each HTTP probe creates a new `httpx.AsyncClient`. Same pattern as litellm_client. Acceptable for 300s intervals

#### controller/sources/submariner_imports.py

- **[FIXED]** `WORKLOAD_ID_LABEL` redefined locally → now imported from `config.py` (L3)
- **[SMELL: MEDIUM]** Uses `detect_mode(model_id, "")` (backward-compat wrapper) instead of `detect_mode_from_paths`. Submariner imports don't probe OpenAPI, so mode detection is name-based only. This is a deliberate trade-off (cross-cluster HTTP probing is expensive), but it means submariner models may get wrong mode if the model_id doesn't contain mode hints
- **[SMELL: LOW]** Doesn't set `provider` on `OicmModel`. Defaults to `"hosted_vllm"`. If a submariner-imported model is from inception, it won't get the correct provider. The `detect_provider` function is not called

#### controller/fallbacks/service.py

- **[SMELL: HIGH]** `FallbackReconciler.reconcile()` is a no-op. It lists fallbacks and model names, logs if a fallback model isn't registered, but never calls `set_fallback` or removes stale fallbacks. The `set_fallback` method on `FallbackClient` is dead code. The entire fallbacks subsystem is read-only logging

#### controller/fallbacks/client.py

- **[FIXED]** `Optional` unused import removed (L1)
- **[SMELL: LOW]** `set_fallback` method is dead code (never called). Kept for potential future use, but per CLAUDE.md "No features beyond what was asked"

#### controller/pricing/ (all files)

- **[OK]** `PricingEntry`, `MatcherCandidate`, `PricingResult` are frozen dataclasses with `slots=True`. Good
- **[OK]** `PricingIndex` uses `__slots__`. Good
- **[FIXED]** `pricing_to_params` return type: `Optional[dict]` → `Optional[dict[str, float]]`
- **[OK]** Matcher chain (exact → structured → fuzzy → substring) is clean, each returns immutable tuple
- **[OK]** Aggregator handles edge cases (empty, single, multiple, zero weight)
- **[SMELL: LOW]** `aggregator.py` uses `Optional` from typing instead of `X | None` (Python 3.10+). Cosmetic, project may need 3.9 compat
- **[SMELL: LOW]** `resolver.py:41` uses `candidates = []` (mutable list) then `candidates.extend(...)`. Could be a tuple comprehension, but the extend-in-loop pattern is readable

#### Deleted Files

- **[FIXED]** `controller/health.py` — Dead code. Controller has inline `/health` handler. Deleted
- **[FIXED]** `controller/fallbacks/models.py` — Empty whitespace-only file. Deleted

### L3: Cross-Reference Analysis

#### Constant ↔ Usage Cross-Check

| Constant | Defined In | Used In | Status |
|---|---|---|---|
| `WORKLOAD_ID_LABEL` | config.py:17 | controller.py, local_deployments.py, submariner_imports.py | **FIXED** (was duplicated in submariner) |
| `WORKLOAD_TYPE_LABEL` | config.py:16 | controller.py, local_deployments.py, submariner_imports.py | OK |
| `MODEL_DEPLOYMENT_TYPE` | config.py:18 | controller.py, local_deployments.py, submariner_imports.py | OK |
| `CHAT_PATH` | models.py:40 | models.py (detect_mode_from_paths) | OK |
| `TRANSCRIPTION_PATH` | models.py:41 | models.py (detect_mode_from_paths) | OK |
| `TTS_PATH` | models.py:42 | models.py (detect_mode_from_paths) | OK |
| `EMBEDDING_PATH` | models.py:43 | models.py (detect_mode_from_paths) | OK |
| `RERANK_PATHS` | models.py:44 | models.py (detect_mode_from_paths) | OK |
| `CONFIG_KEYS` | reconciler.py:16 | reconciler.py (_pick_richest_entry) | OK (single use) |
| `LIGHTHOUSE_LABEL` | submariner_imports.py:25 | submariner_imports.py | OK |
| `LIGHTHOUSE_VALUE` | submariner_imports.py:26 | submariner_imports.py | OK |
| `SOURCE_CLUSTER_LABEL` | submariner_imports.py:27 | submariner_imports.py | OK |
| `SERVICE_NAME_LABEL` | submariner_imports.py:28 | submariner_imports.py | OK |
| `RESERVED_KEYS` | pricing/source.py:11 | pricing/source.py (_build_entry) | OK |
| `INDEXABLE_MODES` | pricing/source.py:12 | pricing/source.py (_build_entry) | OK |
| `MIN_SUBSTRING_LENGTH` | pricing/matchers.py:115 | pricing/matchers.py (substring_match) | OK |
| `DEFAULT_MATCHERS` | pricing/matchers.py:148 | pricing/resolver.py | OK |

#### Function ↔ Caller Cross-Check

| Function | Defined In | Called From | Status |
|---|---|---|---|
| `detect_mode_from_paths` | models.py:50 | local_deployments.py, controller.py | OK |
| `detect_mode` | models.py:75 | submariner_imports.py only | OK (backward compat) |
| `detect_provider` | models.py:79 | local_deployments.py, controller.py | OK |
| `sanitize_model_id` | models.py:35 | local_deployments.py, submariner_imports.py, controller.py | OK |
| `LiteLLMClient.register_model` | litellm_client.py:96 | controller.py (_handle_add) | OK |
| `LiteLLMClient.deregister_model` | litellm_client.py:102 | controller.py (_handle_delete) | OK |
| `LiteLLMClient.patch_model` | litellm_client.py:106 | **NOWHERE** | **FIXED** (deleted) |
| `LiteLLMClient.batch` | litellm_client.py:54 | reconciler.py, register_model, deregister_model | OK |
| `FallbackClient.set_fallback` | fallbacks/client.py:57 | **NOWHERE** | Dead code (kept for API completeness) |
| `LocalDeploymentSource.discover_model_id` | local_deployments.py:96 | controller.py (_handle_add) | OK |
| `LocalDeploymentSource.get_configmap_field` | local_deployments.py:99 | controller.py (_handle_add) | OK |
| `LocalDeploymentSource.probe_openapi_paths` | local_deployments.py:102 | controller.py (_handle_add) | OK |
| `LocalDeploymentSource.discover_owned_by` | local_deployments.py:105 | controller.py (_handle_add) | OK |
| `start_health_server` | health.py:15 | **NOWHERE** | **FIXED** (file deleted) |

#### Data Flow Gap: Duplicate /v1/models Calls

```
discover() per pod:
    _discover_model_id(uuid)
        └── _query_v1_models(uuid)     ← HTTP GET /v1/models  (call #1, if no ConfigMap)
    _probe_openapi_paths(uuid)          ← HTTP GET /openapi.json (call #2)
    _discover_owned_by(uuid)            ← HTTP GET /v1/models  (call #3, ALWAYS)
```

`_query_v1_models` extracts `data[0]["id"]`. `_discover_owned_by` extracts `data[0]["owned_by"]`. Both hit the same endpoint. When ConfigMap has no MODEL_ID, that's 2 calls to `/v1/models` for the same pod, fetching the same JSON, extracting different fields. They should be a single call returning `(model_id, owned_by)`.

#### Data Flow Gap: Event Path vs Sync Path Divergence

```
full_sync path:                          event path (_handle_add):
    discover()                                discover_model_id()
    → OicmModel(mode, provider)              get_configmap_field()
    → compute_plan()                          probe_openapi_paths()
    → execute() → batch()                    discover_owned_by()
    → _state[uuid] = model                   detect_mode_from_paths()
                                             detect_provider()
                                             → OicmModel(mode, provider)
                                             → register_model() directly
                                             → _state[uuid] = model
```

Both paths build an `OicmModel` with the same fields, but the code is duplicated. If `discover()` changes (e.g., adds a new field or changes detection logic), `_handle_add` must be updated separately. The event path also bypasses the reconciler, meaning:
- No pricing resolution comparison with existing litellm entry
- No duplicate detection (if full_sync is running concurrently)
- No patch path (always registers, never patches)

#### Data Flow Gap: _handle_modify Never Re-detects

```
_handle_modify(uuid, dep):
    old_model = _state.get(uuid)
    if old_model:
        old_model.ready_replicas = ready     ← MUTATION
        old_model.total_replicas = total     ← MUTATION
        # No re-probe of openapi paths
        # No re-detection of mode/provider
        # No litellm API call to update
```

If a deployment's image changes from vLLM to inception (or from chat to TTS), `_handle_modify` only updates replica counts. The mode and provider stay stale until the next `full_sync` (300s) catches the name change and does delete+register.

### L4: Fix-Then-Recheck Results

#### Fixes Applied

| # | File | Fix | Cascade Risk |
|---|---|---|---|
| 1 | models.py | Remove unused `field` import | None |
| 2 | health.py | Delete file (dead code) | None (no imports) |
| 3 | fallbacks/models.py | Delete file (empty) | None (no imports) |
| 4 | __main__.py | `frame` → `_frame` | None |
| 5 | reconciler.py | Remove unused `existing_params` | None |
| 6 | fallbacks/client.py | Remove unused `Optional` import | None |
| 7 | controller.py | `request` → `_request` in `_health` | None |
| 8 | submariner_imports.py | Import `WORKLOAD_ID_LABEL` from config | None |
| 9 | litellm_client.py | Remove `_empty_list`, use `asyncio.gather()` | Verified: gather with empty gen returns `[]` |
| 10 | litellm_client.py | Remove dead `patch_model` | None (no callers) |
| 11 | litellm_client.py | Fix `list_all_models_by_uuid` return type | None |
| 12 | pricing/utils.py | Fix `pricing_to_params` return type | None |
| 13 | models.py | `# noqa: PLR0911` on `detect_mode_from_paths` | None |

#### Post-Fix L1 Results

```
pyflakes: 1 finding (_config side-effect import, intentional, # noqa: F401)
vulture:  clean
ruff:     all checks passed
py_compile: all 19 files compile
```

---

## Part 3: Remaining Smells (Not Fixed in This Pass)

These are architectural issues that require larger changes. Documented for awareness.

### HIGH: Duplicate /v1/models HTTP calls

**Location**: `local_deployments.py` `_query_v1_models` + `_discover_owned_by`
**Impact**: 2 HTTP calls to same endpoint per pod during discover() when ConfigMap lacks MODEL_ID
**Fix**: Merge into single `_query_v1_models` returning `(model_id, owned_by)` tuple

### HIGH: FallbackReconciler is a no-op

**Location**: `fallbacks/service.py`
**Impact**: Fallback subsystem does nothing beyond logging. `set_fallback` is dead code
**Fix**: Either implement fallback reconciliation (add/remove fallbacks based on registered models) or remove the subsystem entirely if not needed

### MEDIUM: _handle_add duplicates discover() logic

**Location**: `controller.py:170-213` vs `local_deployments.py:42-79`
**Impact**: Two parallel discovery code paths that can diverge. Event path bypasses reconciler
**Fix**: Extract a shared `_build_model(uuid, dep)` method on `LocalDeploymentSource` that both `discover()` and `_handle_add` call. Route event-driven adds through the reconciler instead of direct `register_model`

### MEDIUM: _handle_modify doesn't re-detect mode/provider

**Location**: `controller.py:222-231`
**Impact**: Image changes (vLLM → inception, chat → TTS) not reflected until next full_sync (300s)
**Fix**: On modify, re-probe openapi paths and re-detect mode/provider. If changed, delete + re-register via litellm

### MEDIUM: LiteLLMClient creates new httpx.AsyncClient per call

**Location**: `litellm_client.py` (batch, list_all_models_by_uuid)
**Impact**: No connection reuse. Under burst conditions (rapid deployment events), creates many TCP connections
**Fix**: Create a shared `httpx.AsyncClient` in `__init__` with connection pooling, close in `stop()`

### MEDIUM: Submariner source doesn't detect provider

**Location**: `submariner_imports.py:99`
**Impact**: Submariner-imported inception models get `provider="hosted_vllm"` instead of `"inception"`
**Fix**: Call `detect_provider` with owned_by from `/v1/models` (already fetched for model_id)

### LOW: SyncPlan uses mutable dataclass fields

**Location**: `reconciler.py:30`
**Impact**: Violates immutability principle from coding guidelines
**Fix**: Requires significant restructuring (builder pattern or functional compute + separate execute)

### LOW: list_all_models_by_uuid swallows errors

**Location**: `litellm_client.py:38`
**Impact**: If litellm proxy is temporarily down, returns `{}`, causing full_sync to re-register all models (duplicates)
**Fix**: Raise the exception and let full_sync skip the cycle, or return a sentinel that compute_plan recognizes as "litellm unavailable"
