# OICM Custom Code — Vertical Slice Locations & Pattern

> **Problem**: OICM custom features (voice routes, voice/script SDK, custom
> provider config, Prometheus in-flight gauge, HTB rate limiter, team cache
> invalidation, model-performance rollup) used to live **inline inside upstream
> files** (`proxy_server.py`, `main.py`, `prometheus.py`, `utils.py`). Every
> upstream merge conflicted on those giant files and risked silently dropping a
> custom feature.

> **Solution:** Extract each OICM feature into a **co-located vertical slice** —
> a self-contained module next to the code it extends, mirroring upstream's own
> `litellm/endpoints/<feature>/` layout — and re-export it through the public
> surface. The runtime wiring (mount lines, re-exports, callback registrations)
> is pinned by drop-detection tests so a future merge that drops a slice fails a
> test instead of failing in production.

This page is the **single source of truth for where OICM custom code now lives**
after the `v1.97.0` merge, what pattern each slice follows, and how it is wired
into the runtime. If you are about to edit an OICM feature, start here.

---

## The vertical-slice pattern

OICM custom code is extracted into a self-contained module and re-exported
through the public surface, leaving the upstream-managed parent file with only a
thin import/delegation line.

1. **Co-locate.** Put the slice next to what it extends, mirroring upstream's own
   layout (`litellm/endpoints/voice/` mirrors
   `litellm/endpoints/speech/speech_to_completion_bridge`).
2. **Keep the public API.** `litellm.acreate_voice` etc. must still resolve.
   Re-export through `litellm/__init__.py` or the parent module so callers
   don't change.
3. **Pin the wiring with a drop-detection test.** Add a case to
   `tests/test_litellm/proxy/test_oicm_drop_detection.py` that asserts the mount /
   re-export / callback registration still resolves. A future merge that deletes
   the slice or its wiring fails the test.

---

## Slice map (post-v1.97.0)

### Voice routes — `litellm/proxy/voice_routes.py`

- **Moved from:** inline route definitions in `litellm/proxy/proxy_server.py`
  (768 lines extracted).
- **Pattern:** a `router` object (FastAPI `APIRouter`) holding all OICM voice
  routes, mounted on the app.
- **Wiring:** `app.include_router(voice_routes.router)` in `proxy_server.py`.
- **Drop test:** `test_voice_routes_router_is_mounted_on_app`,
  `test_voice_routes_module_has_router`.

### Voice / script SDK — `litellm/endpoints/voice/`

```
litellm/endpoints/voice/
├── __init__.py        ← re-exports create_voice, acreate_voice, script, ascript
└── main.py            ← the slice implementation (282 lines, moved from litellm/main.py)
```

- **Moved from** `litellm/main.py` (255 lines removed).
- **Public API unchanged:** `litellm.create_voice`, `litellm.acreate_voice`,
  `litellm.script`, `litellm.ascript` still resolve, re-exported lazily via
  `litellm/__init__.py.__getattr__`.
- **Pattern:** mirrors upstream `litellm/endpoints/speech/speech_to_completion_bridge`.
- **Drop test:** `test_voice_sdk_slice_wired_into_litellm_namespace` asserts each
  name resolves and its `__module__` starts with `litellm.endpoints.voice`.
- **Test file:** `tests/test_litellm/endpoints/voice/test_voice_sdk_slice.py`.

### OICM provider config dispatch — `litellm/llms/oicm_providers/`

```
litellm/llms/oicm_providers/
├── __init__.py        ← package
└── registry.py        ← get_provider_voice_config and 3 sibling dispatchers
```

- **Moved from** inline branches in `litellm/utils.py` (76 lines removed).
- **What it owns:** configuration dispatch for OICM-custom providers (Hamsa,
  OmniVoice) and OICM-custom branches grafted into the upstream Inception
  provider (text-to-speech and transcription).
- **Pattern:** a `ProviderConfigManager` facade in `litellm/utils.py` delegates to
  the registry, one clean line per method, mirroring the upstream dispatch
  signature.
- **Drop test:** `test_oicm_provider_registry_dispatch_wired`,
  `test_oicm_provider_dispatch_reachable_through_utils`.
- **OICM test:** `tests/test_litellm/llms/oicm_providers/test_oicm_provider_registry.py`.

### Prometheus in-flight deployment gauge — `litellm/integrations/prometheus_helpers/`

```
litellm/integrations/prometheus_helpers/
└── deployment_in_flight.py
    ├── DeploymentInFlightLedger          ← the OICM ledger class
    └── DeploymentInFlightMetricsMixin    ← mixed into PrometheusLogger
```

- **Moved from** `litellm/integrations/prometheus.py` (206 lines moved).
- **Pattern:** a **mixin** (`DeploymentInFlightMetricsMixin`) is mixed into
  `PrometheusLogger`. The ledger class lives in the slice, not grafted into
  `prometheus.py`.
- **Drop test:** `test_prometheus_in_flight_ledger_wired` asserts
  `issubclass(PrometheusLogger, DeploymentInFlightMetricsMixin)` and that
  `async_pre_call_deployment_hook` / `_reconcile_deployment_in_flight` exist.
- **O test:** `tests/test_litellm/integrations/test_prometheus_deployment_in_progress_requests.py`.

### HTB rate limiter — `litellm/proxy/hooks/dynamic_rate_limiter_v3_htb.py`

- **Isolated** in its own module (commit `d9a2a1c0be`).
- **Registration:** the callback name `dynamic_rate_limiter_v3_htb` stays in
  `litellm/proxy/hooks.PROXY_HOOKS` so configs referencing it load.
- **Drop test:** `test_htb_rate_limiter_callback_registered`,
  `test_htb_rate_limiter_module_present`.

### Team key-cache invalidation — `litellm/proxy/management_helpers/`

```
litellm/proxy/management_helpers/
└── team_cache_invalidation.py     ← _invalidate_team_key_caches
```

- **Moved from** inline definition in
  `litellm/proxy/management_endpoints/team_endpoints.py` (39 lines removed).
- **Pattern:** both `team_endpoints.py` and `model_management_endpoints.py`
  **re-import** `_invalidate_team_key_caches` from the slice (not define inline).
- **Drop test:** `test_team_cache_invalidation_slice_wired` asserts the two
  call sites reference the *same* slice function (identity check), and
  `test_team_cache_invalidation_wired_into_model_management`.
- **O test:** `tests/test_litellm/proxy/management_helpers/test_team_cache_invalidation.py`.

### Model Performance rollup (proxy scheduler) — `litellm/proxy/proxy_server.py`

Not a new slice; an OICM scheduler job (`update_model_performance_rollup`) that
aggregates model-performance reads into coarse SQL buckets. In the `v1.97.0`
merge it was **kept alongside** upstream's new SGR `flush_gateway_requests` job
(both present in `proxy_server.py`).

---

## Frontend slice — Model Performance (UI)

`ui/litellm-dashboard/src/components/UsagePage/components/ModelPerformance/`

```
ModelPerformance/
├── api.ts              ← modelPerformanceCall fetch function
├── types.ts            ← ModelPerformanceModel / Response / Scope / Summary / TimePoint
├── index.ts            ← re-exports for the public import
├── ModelPerformanceView.tsx
└── drop_detection.test.ts   ← fails if networking.tsx re-export is removed
```

- **Moved from** the global `ui/litellm-dashboard/src/components/networking.tsx`.
- **Pattern:** co-located vertical slice matching the codebase's feature-folder
  convention. `networking.tsx` now re-exports for backward compatibility.
- **Wiring test:** `drop_detection.test.ts`.
- `useModelPerformance` hook and `EntityUsage` now import from the slice.

---

## Drop-detection tests

The single safety net for all the above is
`tests/test_litellm/proxy/test_oicm_drop_detection.py`. It is intentionally
**shallow** (assert the wiring exists / resolves) rather than full behavioral
tests: the behavioral contract of each slice is covered by its own co-located
test module. Run it after every upstream merge:

```bash
python -m pytest tests/test_litellm/proxy/test_oicm_drop_detection.py -q
```

It covers: voice routes mount, voice/script SDK re-export, OICM provider
dispatch, Prometheus in-flight ledger mixin, HTB callback registration, and
team cache invalidation slice identity.

---

## Rules for new OICM custom code

- **Never graft a new OICM feature inline into an upstream-managed file.** Create
  a co-located slice and add a drop-detection wiring test.
- **Follow upstream's layout** (`litellm/endpoints/<feature>/`) rather than
  inventing a parallel structure.
- **Keep the public API stable** via re-export; callers (and upstream's facade
  classes) should not need to know the slice exists.
- **Reference files by identity** (import from the slice, don't redefine), so a
  merge that duplicates or drops the definition is caught by an identity assert.