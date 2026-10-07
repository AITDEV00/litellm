# Change report: OICM status into the native health table (2026-10-06)

This is the complete record of the change that moved OICM per-model health and
per-source liveness into LiteLLM's native `LiteLLM_HealthCheckTable`, replacing
the old controller-owned heartbeat model rows. It is written so a future reader
can pick up the work with no other context. Read it top to bottom for the story,
or jump to a section for the artifact.

Scope of this report: commits `31409b80db` (feature), `9ccd60de39` (Prisma fix),
`08f54c344a` (docs + pinned dev manifest), all on branch `jya0-v1.102.0` of
`github.com/AITDEV00/litellm.git`, all deployed to dev only. Prod was not touched.

## 1. Why this change exists

The previous design recorded per-source liveness as a *model row*: a synthetic
`oicm-heartbeat-<cluster>` entry with `blocked = true`, carrying a
`model_info.oicm_heartbeat` timestamp. Three problems drove the rewrite:

1. A cluster-scoped fact was wearing a model row's clothes. The liveness
   timestamp is per source, but storing it on a model row duplicates it across
   that source's models conceptually and puts a non-model into the model list.
2. There was no per-model home for OICM truth. `/health/latest`,
   `/health/history`, the Admin UI health column, and native retention all
   already existed and were consumed, but OICM had no way to feed them.
3. A probe would have to reconstruct OICM's answer, badly. OICM's REST API is
   the authoritative source for whether a deployment can serve, and it carries
   richer status (lifecycle word, replicas, error message, status-change time,
   cluster) than a probe would. The decision was to reuse OICM truth rather than
   probe.

The chosen design: keep LiteLLM's native health table and every native reader,
and add two admin-only gateway routes the controller POSTs to. Nothing about
routing changes, because the router's health cache is fed only by the background
probe loop, which stays off.

## 2. Architecture at a glance

```
OICM REST API  (authoritative)
      |
      v
Discovery controller (poll every 10s)
      |  reconcile: register/delete/patch model rows        (unchanged)
      |  status writer:
      |    - PATCH model_info.oicm + blocked  (on change)     (unchanged)
      |    - POST /oicm/v1/status-reports     (on change, else hourly)
      |    - POST /oicm/v1/heartbeats         (every 30s)
      v
LiteLLM proxy (dev / prod)
      |  native save_health_check_result -> LiteLLM_HealthCheckTable
      v
Native readers (unchanged):
  GET /health/latest, GET /health/history, Admin UI health column,
  native retention (maximum_health_check_retention_period)
```

Key invariant: the two new routes only *write* the append-only health table.
They never touch a model row, never reload the router, and never feed the
routing health cache. The health table is presentation-only.

## 3. Gateway changes (the LiteLLM fork)

### 3.1 New file: `litellm/proxy/oicm_routes.py` (184 lines)

A self-contained vertical slice, co-located next to `proxy_server.py` like
`voice_routes.py`. Module constants:

| Name | Value | Meaning |
|---|---|---|
| `_HEALTHY` | `"healthy"` | native vocabulary word |
| `_UNHEALTHY` | `"unhealthy"` | native vocabulary word |
| `_DEFAULT_CHECKED_BY` | `"oicm-controller"` | default `checked_by` / reporter |
| `_SOURCE_ROW_PREFIX` | `"oicm-source-"` | reserved model_name prefix for liveness rows |

Pydantic request models (all `ConfigDict(protected_namespaces=())`):

- `OicmStatusReport`: `model_name` (min_length 1), `litellm_model_id` (opt),
  `healthy` (bool), `error_message` (opt), `details` (opt `dict[str, object]`).
- `OicmStatusReportBatch`: `reporter` (default `oicm-controller`), `reports`
  (list, min_length 1).
- `OicmSourceHeartbeat`: `cluster` (min_length 1), `details` (opt).
- `OicmHeartbeatBatch`: `reporter`, `heartbeats` (list, min_length 1).

Guards:

- `_require_admin(user_api_key_dict)`: raises 403 unless role is `PROXY_ADMIN`.
- `_require_prisma(prisma_client)`: raises 500 if the DB client is `None`.

Route 1: `POST /oicm/v1/status-reports`, `dependencies=[Depends(user_api_key_auth)]`.
Defer-imports `prisma_client` from `proxy_server` (proxy_server imports this
router, so the import must be deferred). For each report it calls the native
`prisma_client.save_health_check_result(...)` with:

- `model_name = report.model_name`
- `model_id = report.litellm_model_id`
- `status = "healthy" if report.healthy else "unhealthy"` (native vocabulary)
- `healthy_count = 1/0`, `unhealthy_count = 0/1`
- `error_message = report.error_message`
- `response_time_ms = None` (OICM gives no probe latency; do not invent one)
- `details = report.details`
- `checked_by = batch.reporter`

All writes go through `asyncio.gather`; failures are counted, not raised (the
native function returns `None` on failure and never raises). Response:
`{"saved": <n>, "received": <m>}`. A short save logs a warning.

Route 2: `POST /oicm/v1/heartbeats`, same auth/guards. For each heartbeat it
writes a native row with:

- `model_name = f"oicm-source-{hb.cluster}"`
- `model_id = None` (a key no native writer can produce, so no collision, and
  the Admin UI latest-checks join skips it since no model id matches)
- `status = "healthy"`, `healthy_count = 1`, `unhealthy_count = 0`
- `details = hb.details`, `checked_by = batch.reporter`

Same `{"saved", "received"}` response shape.

The server stamps `checked_at` (native column default `now()`), so the timestamp
means "when the gateway last heard from the controller" and cannot be forged or
drifted by the controller.

### 3.2 Modified: `litellm/proxy/proxy_server.py` (+6 lines)

Mounted in the fork-appended include block at the bottom, immediately after the
voice router, so upstream merges never touch it:

```python
# OICM status ingestion routes (co-located vertical slice). Writes OICM truth
# into the native health table via /oicm/v1/status-reports and /oicm/v1/heartbeats.
from litellm.proxy.oicm_routes import router as oicm_status_router

app.include_router(oicm_status_router)
```

### 3.3 Modified: `litellm/proxy/utils.py` (the Prisma Json fix)

`PrismaClient._clean_details` now wraps its parsed output in `prisma.Json(...)`:

```python
def _clean_details(self, details: dict | None) -> dict | None:
    """Clean and validate details JSON

    The generated Prisma client rejects plain dicts for `Json?` columns
    (the query engine only accepts `prisma.Json` inputs), so the parsed
    value is wrapped here at the single choke point every health-row
    writer goes through.
    """
    if not isinstance(details, dict):
        return None
    try:
        from prisma import Json  # noqa: PLC0415  # generated client may be absent in tools that never touch the DB

        return Json(safe_json_loads(safe_dumps(details)))
    except Exception as e:
        verbose_proxy_logger.warning("Failed to clean details JSON: %s", e)
        return None
```

This is a real native bug fix, not an OICM-specific one. Before it, ANY health
row carrying `details` failed to persist with:

```
Unable to match input value to any allowed input type for the field.
Parse errors: [Invalid argument type. `details` should be of any of the
following types: `NullableJsonNullValueInput`, ... `Json`]
```

`save_health_check_result` swallows that exception and returns `None`, so the
failure was silent at the API layer and visible only in the proxy error log. The
native background health loop had the same latent bug whenever it wrote `details`.
The heartbeat rows worked before this fix only because they pass `details=None`.

Why `_clean_details` and not the call sites: it is the single choke point every
health-row writer shares (the native loop and both new routes), so one edit fixes
all of them. House precedent for wrapping: `key_management_endpoints.py` uses
`prisma.Json(...)` for `litellm_params`.

### 3.4 New tests: `tests/test_litellm/proxy/oicm_routes/`

`__init__.py` (empty) and `test_endpoints.py` (282 lines, 13 tests):

- `TestStatusReports`
  - `test_each_report_becomes_one_native_save`
  - `test_status_uses_the_native_vocabulary` (only `healthy`/`unhealthy`)
  - `test_serving_truth_rides_in_details_not_status`
  - `test_no_response_time_is_written`
  - `test_failed_saves_are_counted_not_raised` (partial save -> `{"saved":1,"received":2}`)
  - `test_non_admin_is_refused` (403, no writes)
  - `test_reporter_stamps_checked_by`
  - `test_empty_batch_is_rejected_at_validation` (422)
  - `test_serving_false_is_not_required_to_carry_an_error`
- `TestHeartbeats`
  - `test_heartbeat_row_cannot_be_a_model_row` (`model_id is None`, name `oicm-source-alain`)
  - `test_two_sources_write_two_rows`
  - `test_unauthenticated_call_is_refused` (401/403)
- `TestRouteShape`
  - `test_paths_live_in_the_oicm_namespace` (anti-shadow regression: exact paths pinned)

Run: `uv run --python 3.13 python -m pytest tests/test_litellm/proxy/oicm_routes/`
Result: 13 passed.

## 4. Controller changes (oicm-litellm-layer)

### 4.1 `controller/litellm_client.py`

Removed (heartbeat model-row machinery, now dead):

- `upsert_heartbeat`, `_patch_heartbeat`, `list_heartbeats`, `heartbeat_payload`.

Added:

- `report_status(reports: Sequence[dict], *, reporter="oicm-controller") -> bool`.
  Read-only mode logs and returns `False`. Empty list returns `True` (nothing to
  do is success). POSTs `{base_url}/oicm/v1/status-reports` with
  `{"reporter", "reports"}` and returns
  `resp.json().get("saved") == len(reports)` — a short save is a failure, so the
  caller retries the models that were not stored.
- `report_heartbeats(clusters: Sequence[str], *, reporter="oicm-controller") -> Mapping[str, bool]`.
  Read-only returns `{}`; empty returns `{}`. POSTs
  `{base_url}/oicm/v1/heartbeats` with `{"reporter", "heartbeats": [{"cluster": c}]}`.
  Maps `saved_per_cluster` if present, else falls back to all-or-nothing from
  `saved == len(clusters)`. On error returns `{}` (never reports a source alive
  on a failed write). Returns a `MappingProxyType`.

Typing imports widened to include `Mapping`, `Sequence`; added
`from types import MappingProxyType`.

Note (minor, cosmetic): the heartbeats route returns `{"saved", "received"}` and
not `saved_per_cluster`, so the client always takes the all-or-nothing fallback
branch. Both sources succeed or fail together, which is the observable behaviour
today. If per-source partial results ever matter, add `saved_per_cluster` to the
route response and the client already handles it.

### 4.2 `controller/status_persister.py`

Docstring rewritten to describe three writes: block PATCH, per-model health rows,
per-source liveness rows. Changes:

- New constant `HEALTH_REFRESH_SECONDS = 3600` (mirrors native
  `_should_persist_health_check_result`). Removed `HEARTBEAT_NAME_PREFIX`.
- `StatusPersister.__init__(litellm, heartbeat_interval=HEARTBEAT_INTERVAL,
  health_refresh_seconds=HEALTH_REFRESH_SECONDS)`. New state:
  `self._last_health_at: dict[str, float]` (keyed by LiteLLM model id) and
  `self.checked_at: Mapping[str, bool]` (per-cluster last-report success).
- `persist()` order: compute `current` once, `plan_writes` -> `patch_status`
  gather, then `await self._maybe_report_health(...)`, then
  `await self._maybe_heartbeat(..., now=current)`.
- `_maybe_report_health(...)`: for each snapshot entry with a `model_id`, skip
  when `last is not None and (now - last) < self.health_refresh_seconds`
  (first cycle always reports). Report dict:
  `{"model_name": entry.get("model_name") or model_id, "litellm_model_id":
  model_id, "healthy": snapshot.serving_available, "error_message":
  snapshot.error_msg, "details": {**block, "cluster": snapshot.cluster}}`. Only
  on `report_status` success does it set `_last_health_at[id] = now`, so a failed
  batch retries next cycle.
- `_maybe_heartbeat(...)`: skipped when there are no snapshots; rate-limited by
  `heartbeat_interval`; `clusters = sorted({...})`;
  `results = await self.litellm.report_heartbeats(clusters)`; on non-empty
  results sets `self.checked_at = MappingProxyType(dict(results))`.

Critical detail learned the hard way: `_last_health_at` must advance ONLY on a
successful `report_status`. An earlier version advanced it in `persist()` after
the PATCH loop, which made the very first health report get skipped. A debug
trace (`report_status awaited 0`, `_last_health_at={'id-1': ...}`) exposed it.

### 4.3 `controller/controller.py`

`/status` body key changed from `"checked_at": self._checked_at(snap.cluster)` to
`"source_alive": self._source_alive(snap.cluster)`. Method `_checked_at` renamed
to `_source_alive`, returning `Optional[bool]`: whether the last liveness report
for that cluster reached the gateway. The authoritative timestamp is the
gateway's own server-stamped `checked_at`, so the controller reports only whether
its last report succeeded, not a second clock that could drift.

### 4.4 Tests

`tests/controller/test_status_persister.py` (rewritten heartbeat tests + new
health tests):

- `TestHeartbeat`: one heartbeat per source not per model; rate-limited to its
  own cadence; no heartbeat without snapshots; failed heartbeat not recorded as
  alive (`checked_at == {}`).
- `TestHealthReports`: first cycle reports every registered model; steady state
  reports nothing; stable model refreshed after `HEALTH_REFRESH_SECONDS + 1`;
  serving flip reported even without the patch; report carries the block and
  cluster; snapshot without a gateway row is not reported; failed report retries
  next cycle (`side_effect=[False, True]`).

`tests/controller/test_litellm_client.py` (4 appended):

- `test_report_status_posts_one_batch_and_requires_full_save`
- `test_report_status_empty_batch_writes_nothing`
- `test_report_heartbeats_maps_per_cluster_results`
- `test_report_heartbeats_failure_returns_empty_not_false`

Run: `cd oicm-litellm-layer && uv run --extra test --python 3.13 python -m pytest tests/controller`
Result: 300 passed, 2 pre-existing `test_config.py` failures (stale prod manifest
key expectations, unrelated to this change).

Mutation testing (3 mutations, all killed):

| Mutation | Killed by |
|---|---|
| refresh rule removed (`if last is not None: continue`) | `test_stable_model_is_refreshed_after_an_hour` |
| healthy inverted (`not snapshot.serving_available`) | `test_first_cycle_reports_every_registered_model`, `test_serving_flip_is_reported_even_without_the_patch` |
| success bookkeeping removed (always advance) | `test_report_carries_the_block_and_cluster`, `test_failed_report_retries_next_cycle` (+4 more) |

### 4.5 `controller/README.md`

The "Each cluster also gets one controller-owned heartbeat row..." paragraph was
replaced with a description of the two routes, server-stamped `checked_at`,
`checked_by = oicm-controller`, native readers, and the
`oicm-source-<cluster>` freshness key.

## 5. Config and manifests

### 5.1 `deploy/dev/litellm-config-dev.yaml` (dev ConfigMap)

Added under `general_settings`:

```yaml
# Bounds the OICM health rows the controller writes into the native
# health table (per-model reports plus per-source liveness). A month of
# history keeps incident review possible; unset means rows never delete.
maximum_health_check_retention_period: "30d"
```

This is the native retention knob (`_types.py` ~2787): rows whose `checked_at` is
older than the period are deleted by the native spend-log cleanup job, on that
job's schedule. There is no count-based pruning in LiteLLM; this is the only knob.

Applied with:
`kubectl --kubeconfig=$KUBECONFIG -n adeo-litellm apply -f deploy/dev/litellm-config-dev.yaml`
then a rollout restart of `litellm-proxy-dev`.

### 5.2 `deploy/dev/discovery-controller-dev.yaml`

Dev controller image pinned to `0.1.0-20261006-31409b8` by
`make controller-release-dev`. Prod manifest untouched.

## 6. Deploy mechanics used (and the gap)

- Controller to dev: `make controller-deploy-dev` (build, push, pin dev manifest,
  apply, rollout). One command for the whole dev loop.
- Gateway image: `make litellm-src-build-push` (podman build + push, tag derived
  from branch -> `jya0-v1.102.0`).
- Gateway to dev: `make litellm-src-deploy-dev` (added 2026-10-07). It pins the
  current tag in `deploy/dev/litellm-proxy-dev.yaml`, applies the dev ConfigMap
  and dev proxy manifest, and rolls `litellm-proxy-dev` out. Prod's manifest and
  Deployment are never touched. `make litellm-src-release-dev` is the one-shot
  build + push + deploy.
- `litellm-src-deploy` targets PROD (it edits `deploy/prod/litellm-proxy.yaml`
  and restarts the prod proxy) and must not be run for dev work.
- CAVEAT: dev and prod reference the SAME image tag (`litellm-src:jya0-v1.102.0`),
  so a build overwrites the bytes behind that tag for both. `litellm-src-deploy-dev`
  only restarts the dev Deployment, so prod keeps serving its already-running
  container until prod is deliberately restarted. Treat the tag as a dev channel
  until prod is moved to its own tag.

## 7. Live verification on dev (2026-10-06)

Proxy image after rollout: `litellm-src@sha256:fa1d87e0...` (tag `jya0-v1.102.0`).
Controller image: `oicm-discovery-controller:0.1.0-20261006-31409b8`.

Verified:

- Both routes registered: `app.routes` contains `/oicm/v1/heartbeats` and
  `/oicm/v1/status-reports`.
- `GET /health/latest` returns 25 per-model rows plus 2 `oicm-source-*` rows.
  Every row `checked_by = oicm-controller`; per-model `details` carry the OICM
  block (`status`, `serving_available`, `replicas`, `cluster`); source rows carry
  fresh server-stamped `checked_at` within 30s.
- Unauthenticated POST to `/oicm/v1/status-reports` -> 401.
- Steady-state write volume: one heartbeat POST per source per 30s; status
  reports only on change or hourly refresh. During the proxy restart window the
  controller retried the failed status batch each 10s cycle and then went quiet
  once a batch saved all 25 rows, exactly as designed.
- Old `oicm-heartbeat-*` model rows are gone (27 models, no leaks in
  `/v1/models`; the reconciler deleted them because they no longer match OICM
  state, and nothing recreates them).
- The `prisma.Json` fix was confirmed by direct create against the live DB
  (`OK with prisma.Json: <id>`), and by the 25 rows landing after the fix.

## 8. Files changed (complete list)

```
litellm/proxy/oicm_routes.py                                      +184 (new)
litellm/proxy/proxy_server.py                                     +6
litellm/proxy/utils.py                                            +12/-4
tests/test_litellm/proxy/oicm_routes/__init__.py                  new (empty)
tests/test_litellm/proxy/oicm_routes/test_endpoints.py            +282 (new)
oicm-litellm-layer/controller/litellm_client.py                   +/- (report_status, report_heartbeats added; heartbeat methods deleted)
oicm-litellm-layer/controller/status_persister.py                 +/- (health report + heartbeat switch)
oicm-litellm-layer/controller/controller.py                       (source_alive)
oicm-litellm-layer/controller/README.md                           (route contract)
oicm-litellm-layer/tests/controller/test_litellm_client.py        +59
oicm-litellm-layer/tests/controller/test_status_persister.py      +/- (heartbeat rewrite + health tests)
oicm-litellm-layer/deploy/dev/discovery-controller-dev.yaml       (image pin)
oicm-litellm-layer/deploy/dev/litellm-config-dev.yaml             +4 (retention)
oicm-litellm-layer/docs/oicm-status/DESIGN-STATUS-PERSISTENCE.md  (health-table design)
oicm-litellm-layer/docs/oicm-status/IMPLEMENTATION-CHECKLIST.md   (Step 12)
oicm-litellm-layer/docs/oicm-status/PROGRESS-AND-PAUSED-WORK.md   (status)
```

## 9. Data model reference

`LiteLLM_HealthCheckTable` (schema.prisma ~1231), append-only history:

| Column | Type | Notes |
|---|---|---|
| `health_check_id` | String @id uuid | |
| `model_name` | String | OICM: the LiteLLM model name, or `oicm-source-<cluster>` for liveness |
| `model_id` | String? | OICM: the LiteLLM model id; `None` for liveness rows |
| `status` | String | native vocabulary: `healthy` / `unhealthy` |
| `healthy_count` | Int @default(0) | 1 when healthy |
| `unhealthy_count` | Int @default(0) | 1 when unhealthy |
| `error_message` | String? | truncated to 500 by the native writer |
| `response_time_ms` | Float? | always `None` from OICM |
| `details` | Json? | OICM truth block (`status` word, `serving_available`, `replicas`, `cluster`, ...) |
| `checked_by` | String? | `oicm-controller` |
| `checked_at` | DateTime @default(now()) | server-stamped |
| `created_at` / `updated_at` | DateTime | |

Indexes include `(model_id, model_name, checked_at DESC)` used by `/health/latest`.

## 10. Route contract reference

`POST /oicm/v1/status-reports` (admin only)

```json
{
  "reporter": "oicm-controller",
  "reports": [
    {"model_name": "zai-org/GLM-5.2-FP8", "litellm_model_id": "<uuid>",
     "healthy": true, "error_message": null,
     "details": {"v": 1, "status": "Ready", "serving_available": true,
                 "replicas": {"desired": 1, "available": 1}, "cluster": "alain"}}
  ]
}
```

Response `{"saved": 1, "received": 1}`. `saved < received` means some rows did not
persist; the caller retries the whole batch next cycle.

`POST /oicm/v1/heartbeats` (admin only)

```json
{"reporter": "oicm-controller", "heartbeats": [{"cluster": "alain"}]}
```

Response `{"saved": 1, "received": 1}`.

Auth: `Authorization: Bearer <admin/master key>`. Missing/insufficient -> 401/403.
DB unavailable -> 500.

## 11. How to continue (open threads)

1. **openrouter_compat Steps 13-18 (next).** `/api/v1/models/{author}/{slug}/endpoints`
   status enrichment must now read the native health table
   (`/health/latest` plus the `oicm-source-<cluster>` freshness), NOT the old
   heartbeat model rows. `ModelInfo` is `extra=allow`, so `model_info.oicm` and
   `model_info.oicm_cluster` already survive into descriptors; the status fields
   are the remaining work.
2. **Prod release carries the Prisma fix.** Prod runs `:latest` and has the same
   latent `details` bug. The next prod release flow will ship the fix; no prod
   action was taken here.
3. **Optional `saved_per_cluster`.** Add it to the heartbeats route response if
   per-source partial results ever matter; the client already prefers it.

5. **Stale `test_config.py` failures.** Two pre-existing failures
   (`test_admin_key_defaults_to_manifest_value`,
   `test_master_key_from_manifest_reads_prod_manifest`) expect a stale prod
   manifest key. Unrelated to this change but worth fixing.
6. **Flagged, awaiting a decision (not this change):** a non-LLM flood-compute
   deployment registered as chat (`698c3cfd`), hamsa-native pods registered as
   `hosted_vllm` (`cd2850fc`, `9c57bce9`), and dev proxy DB `ON CONFLICT` errors
   on spend rollups (missing unique constraints).

## 12. Reproduce / verify

Gateway tests:
`uv run --python 3.13 python -m pytest tests/test_litellm/proxy/oicm_routes/`

Controller tests:
`cd oicm-litellm-layer && uv run --extra test --python 3.13 python -m pytest tests/controller`

Lint (controller):
`/tmp/lint-env/bin/ruff check --select F,E9,PLC,PLE,PLR,PLW,B,SIM,RET --ignore PLR0913,PLR2004,SIM117,ARG002,ARG003,ARG005 controller/ tests/controller/`
(`B008` for route `Depends()` is house style; `voice_routes.py` has 13 of them.)

Live check (dev):
`export KUBECONFIG=$HOME/.kube/alain-oicm.conf`, master key from
`secret/litellm-master-key-dev`, then `GET /health/latest` on the dev proxy and
confirm 25 per-model rows + 2 `oicm-source-*` rows with fresh `checked_at`.

## 13. Commit log

```
08f54c344a docs(oicm): record the health-table heartbeat design and dev rollout
9ccd60de39 fix(proxy): wrap health-check details in prisma.Json so rows persist
31409b80db feat(oicm): report status into the native health table, drop heartbeat rows
```

Pushed to `origin/jya0-v1.102.0`.
