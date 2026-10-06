# OICM → OpenRouter Model Status — Implementation Checklist

Status: ready to implement. All external unknowns resolved (see `FEASIBILITY-ANSWERS.md` + `evidence/`). This document is the step-by-step execution plan, grounded in the actual code. Each step names the file it touches.

Conventions: **[C]** = controller change (`oicm-litellm-layer/controller/`), **[L]** = LiteLLM change (`litellm/proxy/`), **[D]** = deploy/config. Test = the acceptance check that must pass before the step is done.

Milestone split (do not reorder): **M1** trustworthy model status → **M2** current engine load → **M3** historical OpenRouter statistics.

Storage design for Steps 10-11 (what goes in `model_info.oicm`, how the upsert is gated, and how a `Stopped` deployment stays visible while becoming unroutable): see `DESIGN-STATUS-PERSISTENCE.md`.

Current progress, the OICM join-key blocker, and the next step: see `PROGRESS-AND-PAUSED-WORK.md`.

---

## Milestone 1 — Trustworthy model status

### Step 1 [C] — Freeze the internal facts DTO
Create `controller/oicm_status.py` with `OicmStatusSnapshot` (facts only, no presentation words like "online"):
`workspace_id, workload_id, workload_run_id, source_status, workload_status, health_supported, is_ready, health_message, desired_replicas, available_replicas, error_msg, source_version, source_updated_at, previous_source_status, previous_workload_run_id, status_changed_at, observed_at`
- Use a frozen dataclass / pydantic model, `ReadOnly` fields where a TypedDict is used, per repo rules.
- `observed_at` set by controller at fetch; `status_changed_at` computed by controller (never from OICM `_updated_at`).
- Test: construct from the captured fixtures in `docs/oicm-status/evidence/` (`deployment-detail.json`, `deployment-health.json`, `workload-run.json`); assert all fields populate and `Ready`+`is_ready=false` stays two distinct signals.

### Step 2 [C] — Enrich `OicmModel` with OICM identity
Edit `controller/models.py` (`OicmModel`, line ~45) to carry `workspace_id`, `workload_id`, `workload_run_id`.
- Source labels: `oip/workspace-id`, `oip/workload-id` (already `WORKLOAD_ID_LABEL` in `controller/config.py`), `oip/workload-run-id`. Add a `WORKSPACE_ID_LABEL` / `WORKLOAD_RUN_ID_LABEL` constant in `config.py` next to the existing ones.
- Treat `deployment_id == workload_id` (proven); do NOT add an id-resolution layer.
- Behavior when a label is missing: keep `workload_run_id` Optional and log; `workspace_id`/`workload_id` required — if `workload_id` missing the deployment is already skipped today (`controller.py` `_watch_once`).
- Populate them in `controller/sources/` where `OicmModel` is built from the K8s Deployment (`local` source), reading `dep.metadata.labels`.
- Test: one live K8s Deployment → `OicmModel` preserves all three ids + `api_base` unchanged.

### Step 3 [C] — Implement `OicmClient`
Create `controller/oicm_client.py`, modeled on `controller/litellm_client.py` (httpx.AsyncClient, semaphore-bounded concurrency via `HTTP_CONCURRENCY`).
- Config (new in `config.py`): `OICM_BASE_URL`, `OICM_AUTH_URL`, `OICM_REALM=adeo`, `OICM_CLIENT_ID=adeo`, `OICM_USERNAME`, `OICM_PASSWORD`, `OICM_AUTH_GRANT_TYPE=password`, `OICM_TIMEOUT`, `OICM_CONCURRENCY`.
- Token lifecycle: acquire via password grant to `{OICM_AUTH_URL}/realms/{realm}/protocol/openid-connect/token`; cache; read `expires_in` from the response and refresh proactively (~30s early); on a 401 invalidate, refresh exactly once, retry once.
- Methods (typed returns → Step 1 DTO inputs):
  - `get_deployment(workspace_id, workload_id)`
  - `get_deployment_health(workspace_id, workload_id)`
  - `get_workload_run(workspace_id, workload_id, workload_run_id)`
  - `list_deployments(workspace_id)` (for the cheap periodic pass)
- Do NOT implement the SSE events stream or `inference_metrics` in M1.
- Never log password/token; ensure exceptions don't interpolate the token.
- Test: token cached across calls (one auth for N requests); 401 triggers exactly one refresh+retry; password absent from logs; bounded concurrency honored.

### Step 4 [C][D] — Wire secrets + config
Add the OICM env vars to the controller Deployment manifests in `oicm-litellm-layer/deploy/{prod,dev}/`.
- Source `OICM_USERNAME`/`OICM_PASSWORD` from a K8s Secret via `secretKeyRef` (mirrors the existing `litellm-master-key` pattern referenced in `config.py::_master_key_from_manifest`). Create the secret out-of-band from the local credentials file; never commit the value.
- Test: pod env resolves from secret; no secret in `kubectl describe` output beyond the ref name; `git status` shows no committed secret.

### Step 5 [C] — Status snapshot builder
Create `controller/status/builder.py`: pure function `(workspace_id, summary, previous|None) -> OicmStatusSnapshot`. Zero HTTP/LiteLLM code.
- Maps `status`→`source_status`, `replicas`→`desired_replicas`, `status_detail[]`→`available_replicas`/`unavailable_replicas`/`serving_available`, `error_msg`, `_updated_at`→`source_updated_at`.
- `serving_available` is False for the terminal `Stopped`/`Failed` statuses regardless of `status_detail` (a Stopped deployment returns an empty `status_detail`; the status is what makes the answer correct).
- Test: from fixtures; `Ready`+pod-ready is serving; `Deploying` with `unavailable_replicas=1` is not; `Stopped` with empty `status_detail` is not.

### Step 6 [C] — One workspace call, not a per-deployment fan-out
Use `GET /workspaces/{ws}/deployment_summary` once per cycle. It returns every deployment already carrying `status`, `error_msg`, `replicas`, and the per-Pod/Deployment `status_detail[]`, so the former `get_deployment` + `get_deployment_health` + `get_workload_run` fan-out (1 + 3N calls) is unnecessary. `StatusSource` exposes a single `summaries(workspace_id)`.
- The earlier "cheap vs rich fetch policy" is deleted: one call has nothing to triage.
- `workload_status` and `workload_run_id` are dropped: no consumer used them, and the run id is stable across a whole lifecycle (see Step 15).
- Test: a refresh issues exactly 1 OICM call regardless of deployment count.

### Step 7 [C] — Periodic status poll, 10s
`controller/status_poller.py` runs `StatusPoller.run()` alongside the watch and resync loops. `STATUS_SYNC_INTERVAL` (default 10s) is the only cadence knob; one call covers every deployment, so the interval is independent of the model count.
- Cadence rationale: OICM's own status pipeline is a 5s DB sync plus a 10s informer reload, so polling faster than 10s adds load without fresher data. There is no push channel for deployment status (the only SSE stream carries pod events and is itself a 5s DB poll), so polling is the correct and only option.
- On failure, retain the previous snapshot map and log; never map a fetch failure to `offline`.
- Disabled when `OICM_WORKSPACE_ID` is unset (there is no workspace-list endpoint to discover it).
- Test: one call per refresh; failed poll retains previous snapshots; disabled without a workspace.

### Step 8 [C] — Expose the polled status
The controller's health server serves `GET /status` returning the latest snapshot per workload (status, serving, replicas, error), so every status is viewable without a LiteLLM round trip.
- Test: `/status` returns one entry per polled deployment with a non-null status.

### Step 9 [C] — Transition memory
`StatusPoller` keeps the previous snapshot per workload and passes it into `build_snapshot`, which computes `status_changed_at` when `source_status` or `serving_available` changes and records `previous_source_status`.
- Persistence of the previous snapshot in LiteLLM `model_info.oicm` (so it survives restarts) is deferred to Step 10.
- Test: unchanged facts keep `status_changed_at`; a serving flip moves it even when `source_status` stays `Ready`.

### Step 10 [C] — Persist the complete `model_info.oicm` block
Add a `patch_model_info(model_id, oicm_block)` method to `LiteLLMClient` (separate from full model reconciliation) calling `/model/{id}/update`.
- Always PATCH the **whole** `oicm` object (LiteLLM shallow-merges `model_info`; a partial nested patch would drop keys). The block mirrors the Step-1 DTO.
- Never put the OICM password/token in `model_info`.
- Test: PATCH with the full block; a subsequent read shows all nested keys intact; a partial patch would have dropped keys (regression guard).

### Step 11 [C] — Write-amplification guard
In the refresh path, diff meaningful fields before PATCHing.
- PATCH immediately when status facts change.
- If nothing changed, refresh persisted `observed_at` at most once per periodic cycle, not per K8s MODIFIED.
- Keep this status PATCH independent of the model-config reconciliation diff in `reconciler.py` (`SyncReconciler.compute_plan`) so `observed_at` churn doesn't make the main reconciler see a perpetually-dirty model.
- Test: repeated identical observations → no PATCH; only `observed_at` updates on the periodic cadence.

### Step 12 [C] — OICM failure = staleness, not outage
On OICM timeout/5xx: retain last snapshot, do not advance `observed_at`, log the collection failure. Never map a fetch failure to `offline`.
- Test: kill OICM connectivity → `model_info.oicm.observed_at` stops advancing, `source_status` unchanged; LiteLLM later computes stale.

### Step 13 [L] — Replace the `/endpoints` debug body with official OpenRouter schema
Edit `litellm/proxy/openrouter_compat/models_service.py::get_model_endpoints` (currently returns a custom dict at lines ~84-130) and `routes/models.py`.

**Package confirmed**: `openrouter==1.1.28` (Speakeasy-generated) is installed at `.venv/.../openrouter/`, declared in `pyproject.toml` (`openrouter>=1.2.0,<2.0` — note installed 1.1.28 is below the floor, reconcile this). The existing boundary is `litellm/proxy/openrouter_compat/openrouter_schema/models.py`, which re-exports official SDK component models with a docstring "Only the mapper layer uses these." Add a sibling `openrouter_schema/endpoints.py` re-exporting, all present in `openrouter/components/`:
- `publicendpoint.PublicEndpoint` + `Pricing` (nested; `completion`/`prompt` required strings, the rest Optional)
- `listendpointsresponse.ListEndpointsResponse` (fields: `architecture`, `created`, `description`, `endpoints`, `id`, `name`) + its nested `Architecture`
- `endpointstatus.EndpointStatus` = `Union[Literal[0,-1,-2,-3,-5,-10], UnrecognizedInt]`
- `percentilestats.PercentileStats` (`p50,p75,p90,p99` floats, all required)
- `quantization.Quantization`, `providername.ProviderName`, `parameter.Parameter` (Literal unions)

**`PublicEndpoint` required vs optional** (confirmed from source): required = `context_length, latency_last_30m, max_completion_tokens, max_prompt_tokens, model_id, model_name, name, pricing, provider_name, quantization, supported_parameters, supports_implicit_caching, tag, throughput_last_30m, uptime_last_1d, uptime_last_30m, uptime_last_5m`; Optional = `status`, `supports_voice_cloning`. Note `latency_last_30m / throughput_last_30m / uptime_* / max_*_tokens / quantization` are typed `Nullable[...]` — the model_serializer drops them from JSON when `None` (nullable fields), so **absent metrics serialize as omitted, not null**; the historical fields can be left `None` in M2 cleanly. `pricing` itself is required (only `completion`+`prompt` inside it are).

The generated base uses a `@model_serializer` that strips `UNSET_SENTINEL` and None-nullable fields, so unknown extra keys are not part of the contract — attach `gateway_status` by **composition**, not subclassing (build a wrapper that serializes the official `PublicEndpoint` then adds the `gateway_status` key).

- Confirm `get_model_endpoints` resolves the **individual deployments** (`model.deployments` → `DeploymentDescriptor`, already present) — one LiteLLM/OICM deployment = one `PublicEndpoint`.
- Test: response parses with the official OpenRouter SDK; one deployment per endpoint entry; absent `latency_last_30m` is omitted from JSON, not `null`.

### Step 14 [L] — Implement `OpenRouterEndpointsMapper`
Create `litellm/proxy/openrouter_compat/mapping/endpoints.py` (alongside `mapping/openrouter.py::OpenRouterModelMapper`). Input: one `DeploymentDescriptor` + its `model_info.oicm` + optional telemetry. Output: `PublicEndpoint` + `gateway_status`.
- Confirm `model_info.oicm` reaches `DeploymentDescriptor` (the resolver already surfaces `model_info`).
- Keep OpenRouter-owned `status` integer semantics untouched.
- Test: mapper emits a valid `PublicEndpoint`; `gateway_status` attached separately.

### Step 15 [L] — `gateway_status` extension model + state resolver
Add the extension to the mapper:
`gateway_status: {availability, lifecycle, stale, source, source_status, healthy, replicas:{desired,available}, observed_at}`.
- Centralize policy in one `GatewayStateResolver`:
  `Ready`+`serving_available=true`→online/stable; `Ready`+`serving_available=false`→degraded/stable; `Stopped`→offline/stopped; `Deploying`→offline/deploying; `Available`→online/stable; `Failed`→offline/failed; stale→unknown.
- **`serving_available` replaces the old `is_ready` signal.** OICM's `/health.is_ready` was advisory and stale (recomputed only on lifecycle events), and `serving_available` comes from the same `status_detail` the live K8s pod readiness produces. There is no separate `is_ready` field to consult.
- **Do not use a `workload_run_id` change for `restarting`.** Live evidence (see `evidence/lifecycle-transitions.json`) shows the run id is stable across a whole `Deploying -> Ready -> Stopped` lifecycle; it changes only when a new lifecycle starts, which `status` already shows as `Ready -> Deploying`. Use the `status` transition, and for a pod-level restart within a run use the `serving_available` flip, never a run-id change.
- Test: the transition matrix; stale → unknown; a `Ready` deployment whose pod drops out of service yields degraded, not online.
- Test: the full transition matrix from `FEASIBILITY-ANSWERS.md`; stale → unknown.

### Step 16 [L] — Freshness at request time
Never persist `stale`. Compute `stale = now - observed_at > STATUS_STALE_AFTER` in the mapper at serve time. `STATUS_STALE_AFTER` configurable.
- Test: old `observed_at` → `stale=true`, `availability=unknown`; fresh → `stale=false`.

### Step 17 [L] — Preserve auth/visibility semantics + missing-model behavior
Apply the existing model visibility/authorization rules before exposing a deployment in `/endpoints`.
- unknown logical model → 404; known model with no deployments → `{…, "data": []}`; unauthorized → same concealment as `/models`.
- Test: unauthorized caller gets the same concealment as `/models`; empty deployment list returns valid `[]` not 500.

### Step 18 [D] — M1 end-to-end validation on dev
Deploy controller + LiteLLM to **dev** (never prod first) via the established flow (`make litellm-src-build/push`, `kubectl rollout restart deploy/litellm-proxy-dev`).
- Drive real transitions: initial deploy→online; restart/redeploy→online; health failure→degraded; stop→offline; OICM down→stale.
- Capture fixtures for the previously-unobserved `Pending`/`Deploying`/`Failed` statuses and add them to `docs/oicm-status/evidence/`.
- Proof via curl against the live dev proxy `/api/v1/models/{author}/{slug}/endpoints`, real provider, real spend path — not pytest screenshots.

---

## Milestone 2 — Current engine load

### Step 19 [L] — `RuntimeTelemetryProvider` abstraction
Create provider interface with `SGLangTelemetryProvider` / `VllmTelemetryProvider`. Deployment runtime is known from `model_server.name/family` (present in the OICM deployment record).
- Test: provider selected by runtime, not by branching in the route.

### Step 20 [L] — Capture real `/metrics` per runtime
Scrape `/metrics` from the actually-deployed SGLang (0.5.19/0.5.18/0.5.16) and vLLM versions. Do not assume one Prometheus schema; note version differences for: running requests, waiting requests, generation throughput, token throughput, KV/cache utilization, request-latency histogram, TTFT, ITL.
- Add captures to `docs/oicm-status/evidence/`.

### Step 21 [L] — Normalize telemetry
Map runtime names → `RuntimeTelemetrySnapshot: {running_requests, queued_requests, generation_tokens_per_second, prompt_tokens_per_second, kv_cache_utilization, observed_at}`. Absent = `None`, never 0.
- Test: unknown metric stays `None`.

### Step 22 [L] — Telemetry cache + single-flight
`/endpoints` must not cause a scrape storm. Target-keyed TTL cache (`deployment_id → snapshot`), single-flight refresh, reusing the `InMemoryDiscoveryCache` pattern (`litellm/proxy/openrouter_compat/cache/memory.py`).
- Multi-replica: each replica owns its small cache, OR shared Redis with one collector. Do NOT combine leader-only collection with a process-local cache.
- Test: concurrent requests → single scrape; TTL expiry refetches.

### Step 23 [L] — Wire telemetry into `gateway_status.requests` / `gateway_status.engine`
Expose genuine instantaneous data only (`requests.running`, `requests.queued`, `engine.generation_throughput`, KV util). Telemetry failure degrades gracefully: lifecycle stays correct, telemetry fields drop to null; `/endpoints` never 500s on a scrape failure.
- Test: SGLang down → lifecycle correct, telemetry null, 200.

---

## Milestone 3 — Historical OpenRouter statistics

### Step 24 [L] — Omit what you can't honestly provide
Do NOT populate `uptime_last_5m/30m/1d`, `latency_last_30m.p90`, `throughput_last_30m.p50` from a single instantaneous scrape (wrong semantics). The DTO allows this cleanly: those fields are `Nullable[...]` and the serializer **omits them from JSON when `None`** (confirmed in `PublicEndpoint.serialize_model`), so leave them `None` in M2 — they won't appear as `null`.
- Test: those fields absent (not `null`) in M2 responses; SDK still parses.

### Step 25 [L] — Real rolling stats from Prometheus/Thanos
If full OpenRouter fidelity is wanted, query genuine 30-min windows (`histogram_quantile`, `increase`, `rate`) from the cluster's Prometheus/Thanos, then populate `latency_last_30m` / `throughput_last_30m` / `uptime_*`.
- Separate milestone; not a prerequisite for useful status.

---

## Cross-cutting tests required before "done"

Controller: token cache/refresh, label extraction (incl. missing `workload_run_id`), DTO parsing from fixtures, `Ready`+`is_ready=false`, run-id change, debounce/coalescing, watch+periodic no double-PATCH, no-op suppression, OICM timeout → staleness, 401 refresh+retry, startup hydration, full nested `oicm` preserved across shallow merge.

LiteLLM: OpenRouter SDK compatibility, multiple deployments per logical model, stale, missing status, stopped, unhealthy-ready, runtime metric failure, absent-metrics-stay-null, authorization filtering, telemetry single-flight, official DTO still parses with `gateway_status` attached.

Per repo rules: tests must fail if the feature breaks (mutation-test mindset), test function not structure, one focused regression over many shallow ones; `tests/test_litellm/` mirrors `litellm/` paths; match the existing test-file naming in the dir you touch.

---

## Doc-consistency fix (carry-over)
`OICM-STATUS-FEASIBILITY.md` lists `GET /workspaces/{ws}/deployments` as 200 in the route table but also names it in the "confirmed non-routes" prose. The captured `evidence/deployments-list.json` is authoritative — remove it from the non-routes list.
