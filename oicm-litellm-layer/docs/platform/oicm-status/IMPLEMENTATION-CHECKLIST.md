# OICM → OpenRouter Model Status — Implementation Checklist

Status: M1's controller half is complete (Steps 3-12). Steps 13-18 (the LiteLLM half of M1) are **done** and deployed to dev, landed in `2c7cf3bb64`. All external unknowns resolved (see `FEASIBILITY-ANSWERS.md` + `evidence/`). This document is the step-by-step execution plan, grounded in the actual code. Each step names the file it touches.

M2 (Steps 19-23) is genuinely not started: there is no `RuntimeTelemetryProvider` in the tree. M3 Step 24 is superseded, and Step 25 shipped (see those steps).

Conventions: **[C]** = controller change (`oicm-litellm-layer/controller/`), **[L]** = LiteLLM change (`litellm/proxy/`), **[D]** = deploy/config. Test = the acceptance check that must pass before the step is done.

Milestone split (do not reorder): **M1** trustworthy model status → **M2** current engine load → **M3** historical OpenRouter statistics.

Storage design for Steps 10-11 (what goes in `model_info.oicm`, how the upsert is gated, and how a `Stopped` deployment stays visible while becoming unroutable): see `DESIGN-STATUS-PERSISTENCE.md`.

Current progress, the roadmap position, and the current problems: see `PROGRESS-AND-PAUSED-WORK.md`. That file carries the authoritative **open work register** (every item still open, verified against the tree, with priorities); this checklist is the step-by-step plan and can lag it.

Some steps below were written before the implementation and now describe an earlier shape than what landed. Those carry a `Status:` line. Do not implement a superseded step as written.

---

## Milestone 1 — Trustworthy model status

### Step 1 [C] — Freeze the internal facts DTO
**Status: superseded by what landed.** The DTO became the `controller/status/` package (`snapshot.py` for `OicmStatusSnapshot`, `wire.py` for the OICM payloads, `builder.py` for the mapping). `controller/oicm_status.py` was never created. The field list below is stale: `is_ready` was replaced by `serving_available` (Step 15), and `workload_status` / `workload_run_id` / `previous_workload_run_id` were dropped in Step 6 as having no consumer.

Create `controller/oicm_status.py` with `OicmStatusSnapshot` (facts only, no presentation words like "online"):
`workspace_id, workload_id, workload_run_id, source_status, workload_status, health_supported, is_ready, health_message, desired_replicas, available_replicas, error_msg, source_version, source_updated_at, previous_source_status, previous_workload_run_id, status_changed_at, observed_at`
- Use a frozen dataclass / pydantic model, `ReadOnly` fields where a TypedDict is used, per repo rules.
- `observed_at` set by controller at fetch; `status_changed_at` computed by controller (never from OICM `_updated_at`).
- Test: construct from the captured fixtures in `docs/oicm-status/evidence/` (`deployment-detail.json`, `deployment-health.json`, `workload-run.json`); assert all fields populate and `Ready`+`is_ready=false` stays two distinct signals.

### Step 2 [C] — Enrich `OicmModel` with OICM identity
**Status: superseded, not done.** No consumer needs the three ids. `deployment_id == workload_id` is the uuid the reconciler already keys on (Step 2 of the design order, `792c09ee60`), and Step 6 dropped `workload_run_id` and `workload_status` outright. Revisit only if a consumer for `workspace_id` or `workload_run_id` appears.

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
**Status: done.** The write is `LiteLLMClient.patch_status`, called by
`StatusPersister`, which `StatusPoller` invokes after each `refresh()`. It rides the
10s poll rather than the 300s config reconcile, so status latency is not tied to
the resync interval.

A status change is **one** PATCH carrying both the top-level column and the nested
object:

```
PATCH /model/{id}/update
{"blocked": true, "model_info": {"oicm": {...}}}
```

- Always send the **whole** `oicm` object (LiteLLM shallow-merges `model_info`, so a
  partial nested patch would drop keys).
- The block carries `v`, `status`, `serving_available`, `gateway_uuid`,
  `replicas`, `status_changed_at`, `observed_at`, `error_msg`. It does **not** carry
  `api_base`: `litellm_params.api_base` already survives independently and is what
  routing reads, so a second copy would only be able to drift. `error_msg` is stored
  in full, uncapped. The cluster is a sibling key `oicm_cluster`, not a block field,
  because it answers "which cluster" at any time rather than being a status fact.
- The same PATCH writes `oicm_cluster`, so a row registered before the field existed
  gains it on its first status write.
- Never put the OICM password/token in `model_info`.
- Test: PATCH with the full block; a subsequent read shows all nested keys intact
  (a partial patch would have dropped keys, so this is the regression guard).

### Step 11 [C] — Write-amplification guard
**Status: done.** The config half is the `compute_plan` idempotence guard
(`13c021c355`). The status half is the fact diff in `StatusPersister`: it compares a
fixed fact set against what the row already carries and writes only when one
differs.

The one field deliberately excluded from that comparison is `observed_at`.
Including it would make every cycle differ, so every cycle would write. It advances
only when some other fact changed, so a steady-state cluster issues zero status
writes and a status change issues exactly one.

- Test: repeated identical observations → no PATCH; a fact change → one PATCH.

### Step 12 [C] — OICM failure = staleness, not outage
**Status: done, reshaped 2026-10-06.** The poller retains a failed source's previous
snapshots and never maps a failure to `offline`. Staleness is expressible because the
per-source liveness timestamp now lives in the native `LiteLLM_HealthCheckTable` as an
`oicm-source-<cluster>` row (written via `POST /oicm/v1/heartbeats`), and per-model health
rides the same table via `POST /oicm/v1/status-reports`, both written by the controller and
read through the native `/health/latest`, `/health/history`, and the Admin UI health
column. The original sentinel model rows (`oicm-heartbeat-*`) are deleted and never
recreated.

- `STATUS_STALE_AFTER = 90s` (three poll intervals, so one slow cycle does not flap
a healthy source to unknown).
- The heartbeat writes every `STATUS_STALE_AFTER / 3` = 30s, not on the 10s poll.
- Health rows write on change, else hourly refresh (mirroring the native background
loop's `_should_persist_health_check_result`), and a failed POST retries next cycle.
- A consumer computes `stale = now - checked_at > STATUS_STALE_AFTER` per source and
  reports `unknown` rather than the persisted status when stale.
- Liveness is per **source**, not per model: one write per source per heartbeat
  instead of one per model, and an Abu Dhabi outage marks only Abu Dhabi unknown.
- Retention is the native knob `maximum_health_check_retention_period` ("30d" on dev).
- Honest limit: a controller that dies and is never replaced cannot be detected,
  since no writer remains. "Past the window, report unknown" is the strongest
  available statement.

See `DESIGN-STATUS-PERSISTENCE.md` for the health-table design and why the old sentinel
model row was the wrong shape.

- Test: kill OICM connectivity → `checked_at` stops advancing, `source_status`
  unchanged; a consumer later computes stale. A stale source reports `unknown` even
  when its last known status was `Ready`.

**Abu Dhabi caveat for `serving_available`: RESOLVED (2026-10-06).** AD OICM `1.7.1` returns `status_detail[]` entries with no `metadata` key at all, only `kind`, `name`, `node`, `status`, and `status_msg`. `metadata` is absent from that version's `StatusDetail` / `WorkloadStatusDetail` / `DeploymentInstance` schemas entirely, while Al Ain's declare it. `status/availability.py::_is_ready` now treats `metadata` as optional and falls back to the entry's own `status` when it is absent (`metadata` still wins when present). Verified live: the Abu Dhabi deployment `766b1720` reads `serving_available: true`. Both clusters are now polled as separate sources; see `PROGRESS-AND-PAUSED-WORK.md`.

### Step 13 [L] — Replace the `/endpoints` debug body with official OpenRouter schema
**Status: done** (`2c7cf3bb64`). The route serves the official `ListEndpointsResponse`; `openrouter_schema/endpoints.py` re-exports the SDK components.

Edit `litellm/proxy/openrouter_compat/models_service.py::get_model_endpoints` and `routes/models.py`.

**Package confirmed**: `openrouter>=1.2.0,<2.0` is declared in `pyproject.toml`, and `1.3.22` is installed, so the earlier "installed 1.1.28 is below the floor" note is resolved. The boundary is `litellm/proxy/openrouter_compat/openrouter_schema/models.py` ("Only the mapper layer uses these"), with the sibling `openrouter_schema/endpoints.py` re-exporting, all present in `openrouter/components/`:
- `publicendpoint.PublicEndpoint` + `Pricing` (nested; `completion`/`prompt` required strings, the rest Optional)
- `listendpointsresponse.ListEndpointsResponse` (fields: `architecture`, `created`, `description`, `endpoints`, `id`, `name`) + its nested `Architecture`
- `endpointstatus.EndpointStatus` = `Union[Literal[0,-1,-2,-3,-5,-10], UnrecognizedInt]`
- `percentilestats.PercentileStats` (`p50,p75,p90,p99` floats, all required)
- `quantization.Quantization`, `providername.ProviderName`, `parameter.Parameter` (Literal unions)

**`PublicEndpoint` required vs optional** (confirmed from source): required = `context_length, latency_last_30m, max_completion_tokens, max_prompt_tokens, model_id, model_name, name, pricing, provider_name, quantization, supported_parameters, supports_implicit_caching, tag, throughput_last_30m, uptime_last_1d, uptime_last_30m, uptime_last_5m`; Optional = `status`, `supports_voice_cloning`. `pricing` itself is required (only `completion`+`prompt` inside it are).

**Nullable fields serialize as explicit `null`, not omitted.** This was originally documented the other way round and is worth stating correctly, because M2/M3 were planned around the wrong assumption. The generated `@model_serializer` keeps a key when it is nullable and explicitly set, so a `None` `latency_last_30m` / `throughput_last_30m` / `uptime_*` / `max_*_tokens` / `quantization` appears in the JSON as `null`. Only `perf_last_30m_by_workload` (optional, never set) is dropped. Verified two ways on 2026-10-07: constructing the mapper output directly, and scraping the live dev payload, both show `latency_last_30m: null` present rather than absent.

The `@model_serializer` strips `UNSET_SENTINEL` and unknown keys are not part of the contract, so `gateway_status` is attached by **subclassing** `PublicEndpoint` and redeclaring `endpoints` on a `ListEndpointsResponse` subclass, which keeps the extra keys in the payload without a cast (see `mapping/endpoints.py`).

- `get_model_endpoints` resolves the **individual deployments** (`model.deployments`) — one LiteLLM/OICM deployment = one `PublicEndpoint`.
- Test: response parses with the official OpenRouter SDK; one deployment per endpoint entry; nullable metrics present as `null`.

### Step 14 [L] — Implement `OpenRouterEndpointsMapper`
**Status: done** (`2c7cf3bb64`). `mapping/endpoints.py::OpenRouterEndpointsMapper` reads the deployments and their statuses; the telemetry it also consumes is described at Step 25.

Create `litellm/proxy/openrouter_compat/mapping/endpoints.py` (alongside `mapping/openrouter.py::OpenRouterModelMapper`). Input: one deployment + its `model_info.oicm` + optional telemetry. Output: `PublicEndpoint` + `gateway_status`.
- `model_info.oicm` reaches the deployment through the resolver's `model_info`.
- OpenRouter-owned `status` integer semantics are untouched (`GatewayStatus.endpoint_status()`).
- Test: mapper emits a valid `PublicEndpoint`; `gateway_status` attached separately.

### Step 15 [L] — `gateway_status` extension model + state resolver
**Status: done** (`2c7cf3bb64`). The DTO is `gateway_status: {oicm_status, availability, stale, source, healthy, replicas:{desired,available}, observed_at, checked_at}`. Note `lifecycle` and `source_status` were dropped from the original shape: `lifecycle` was a lossy grouping of the raw status, and `source_status` duplicated `healthy` instead of carrying the OICM status. The raw OICM status is now `oicm_status`, passed through verbatim.
- `observed_at` is when this model's status was last seen; `checked_at` is when its source was last polled at all. `stale` derives from `checked_at` (per source), never from `observed_at` (per model).
- Policy is centralized in `GatewayStateResolver` (`gateway_status.py`).
- **`serving_available` replaces the old `is_ready` signal.** OICM's `/health.is_ready` was advisory and stale (recomputed only on lifecycle events), and `serving_available` comes from the same `status_detail` the live K8s pod readiness produces. There is no separate `is_ready` field to consult.
- **Do not use a `workload_run_id` change for `restarting`.** Live evidence (`evidence/lifecycle-transitions.json`) shows the run id is stable across a whole `Deploying -> Ready -> Stopped` lifecycle; it changes only when a new lifecycle starts, which `status` already shows as `Ready -> Deploying`. Use the `status` transition, and for a pod-level restart within a run use the `serving_available` flip, never a run-id change.
- Test: the transition matrix from `FEASIBILITY-ANSWERS.md`; stale → unknown; a `Ready` deployment whose pod drops out of service yields degraded, not online.

### Step 16 [L] — Freshness at request time
**Status: done** (`2c7cf3bb64`). `stale` is never persisted; it is computed at serve time from the per-source heartbeat `checked_at`, with `STATUS_STALE_AFTER` defaulting to 90s.
- Test: old `checked_at` → `stale=true`, `availability=unknown`; fresh → `stale=false`.

### Step 17 [L] — Preserve auth/visibility semantics + missing-model behavior
**Status: done** (`2c7cf3bb64`).
- unknown logical model → 404; known model with no deployments → an empty-but-valid endpoint list; unauthorized → same concealment as `/models`.
- A model id with no author segment (e.g. `hamsa-tts`) is canonically namespaced as `litellm/hamsa-tts`. Both URL forms are served: `/api/v1/models/{author}/{slug}/endpoints` and `/api/v1/models/{slug}/endpoints`, returning the same body keyed by the canonical id. The lookup is exact on the canonical id, so a wrong author (`/api/v1/models/wrong/hamsa-tts/endpoints`) is a 404, not a match on the bare slug.
- Test: bare form and namespaced form both resolve to the canonical id; wrong author 404s.

### Step 18 [D] — M1 end-to-end validation on dev
**Status: done.** Verified live on dev: all 28 canonical `/endpoints` URLs return 200, the bare forms return byte-identical bodies, wrong author 404s, unauthenticated 401, unknown model 404. See `docs/openrouter/LOGIC-MAP-2026-10-07-endpoints-consumer.md` §8 for the full scrape.

---

## Milestone 2 — Current engine load

**Not started.** There is no `RuntimeTelemetryProvider` (nor `SGLangTelemetryProvider` / `VllmTelemetryProvider`) in the tree, so Steps 19-23 are all still open. This milestone covers the *instantaneous* engine view (running/queued requests, KV utilization) scraped from each runtime's own `/metrics`. It is distinct from M3, which is the windowed historical statistics and has shipped (Step 25). The reserved `-1` endpoint status (Step 19's rationale in `FEASIBILITY-ANSWERS.md`) stays unassigned until this lands.

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
**Status: superseded.** This step said not to populate the windowed fields from a single instantaneous scrape, and to leave them `None`. Step 25 shipped instead, and the windowed fields are populated from genuine Prometheus 30m windows, so there is nothing to omit. The original assumption that `None` serializes as omitted was also wrong: nullable fields serialize as explicit `null` (see Step 13).

### Step 25 [L] — Real rolling stats from Prometheus/Thanos
**Status: done, with a source change.** The windowed fields are populated from genuine 30-minute Prometheus windows via `histogram_quantile` and `increase`, in `litellm/proxy/openrouter_compat/enrichment/telemetry.py` (`PrometheusDeploymentTelemetryReader`).

What shipped, per field:
- `latency_last_30m`: p50/p75/p90/p99 of `litellm_llm_api_time_to_first_token_metric` (time to first token, reported in ms). Streaming only, since that histogram is observed for streamed requests.
- `throughput_last_30m`: p50/p75/p90/p99 of the inverse of `litellm_deployment_latency_per_output_token` (tokens/second, matching OpenRouter's per-request generation-speed semantics).
- `uptime_last_5m/30m/1d`: `success / (success + failure) * 100`, read from the two counters separately so a deployment with zero failures still reports 100 rather than a missing series.
- Two gateway extensions beyond the OpenRouter contract: `live_concurrency` (from `litellm_deployment_in_progress_requests`, `max by (model_id)` so it is the busiest replica's load) and `requests_last_30m` (a `sum by (model_id)`, since request counts add up across replicas).

**Source change from this step's plan.** The plan named the rollup table / SpendLogs as the source (see `MAPPING-usage-metrics.md` §8h) and this step named Thanos. The implementation reads the cluster Prometheus instead, because the metrics already carry a `model_id` label, which makes per-deployment grouping a plain `sum by (model_id)` with no join, and because the 15s reader cache bounds the query load. The rollup/DB alternative would need a per-deployment key the rollup does not carry. See the decision note at the top of `MAPPING-usage-metrics.md`.

`perf_last_30m_by_workload` remains unimplemented: OpenRouter keys it by workload (`text_generation`/`stt`/`tts`), and we have no metric-to-workload classifier.

- Test: `tests/test_litellm/proxy/openrouter_compat/test_telemetry.py` covers the PromQL construction, the unit transforms, the "all four quantiles or nothing" rule, uptime semantics, and the cache.

---

## Cross-cutting tests required before "done"

Controller: token cache/refresh, label extraction (incl. missing `workload_run_id`), DTO parsing from fixtures, `Ready`+`serving_available=false`, run-id change, debounce/coalescing, watch+periodic no double-PATCH, no-op suppression, OICM timeout → staleness, 401 refresh+retry, startup hydration, full nested `oicm` preserved across shallow merge, cross-cluster `submariner:<cluster>:` prefix join on BOTH sides (so an import is not double-registered), `oicm_cluster` backfilled onto a row that predates it, deletion only when the owning source was polled successfully.

LiteLLM: OpenRouter SDK compatibility, multiple deployments per logical model, stale, missing status, stopped, unhealthy-ready, runtime metric failure, absent-metrics-stay-null, authorization filtering, telemetry single-flight, official DTO still parses with `gateway_status` attached. The telemetry reader adds its own set in `test_telemetry.py` (unit transforms, all-four-or-nothing, uptime, cache TTL, concurrent single-fetch).

Per repo rules: tests must fail if the feature breaks (mutation-test mindset), test function not structure, one focused regression over many shallow ones; `tests/test_litellm/` mirrors `litellm/` paths; match the existing test-file naming in the dir you touch.

---

## Doc-consistency fix (done 2026-10-06)
`OICM-STATUS-FEASIBILITY.md` listed `GET /workspaces/{ws}/deployments` as 200 in the route table but also named it in the "confirmed non-routes" prose. The captured `evidence/deployments-list.json` is authoritative, so the non-routes list now points at the route table instead of naming it. Note this route is the one the design superseded: the controller uses `deployment_summary` (one call for every deployment) rather than the `deployments` list, so the list is a feasibility finding, not the production path.
