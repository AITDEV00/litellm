# Logic map: OICM status ingestion (gateway + controller)

Built with `docs/techniques/logic_mapping_technique.md` (Phase 1: trace) and
audited with `docs/techniques/code_smell_detection_technique.md` (L1-L4).

Scope: the 2026-10-06 health-table change, audited 2026-10-07. Every function in
the flow, with file:line refs, inputs/outputs, and side effects.

## 1. Entry points

| Entry | Where | Auth |
|---|---|---|
| `POST /oicm/v1/status-reports` | `litellm/proxy/oicm_routes.py:106` | `user_api_key_auth` + PROXY_ADMIN |
| `POST /oicm/v1/heartbeats` | `litellm/proxy/oicm_routes.py:151` | `user_api_key_auth` + PROXY_ADMIN |

Both are mounted in `proxy_server.py` (~line 18892) in the fork-appended block.

## 2. Controller flow (10s loop)

```
StatusPoller.run()                              status_poller.py:140
  while running:
    await self.refresh()                        status_poller.py:83
      asyncio.gather(_fetch(s) for s in sources)  status_poller.py:65   # poll OICM, 1 call/source
      build_snapshot(...) per summary           status/snapshot.py
      self.persister.persist_snapshots(...)     status_poller.py:120
    await asyncio.sleep(self.interval)          # STATUS_SYNC_INTERVAL = 10s
```

`StatusPersister.persist_snapshots` (`status_persister.py:170`) reads the gateway
rows, then calls `persist` (`status_persister.py:190`), which makes three
independent write decisions:

```
persist(snapshots, litellm_by_uuid, now)
  |
  +-- plan_writes(...)                          status_persister.py:109
  |     -> StatusWrite per changed model
  |     -> litellm.patch_status(...) x N        litellm_client.py:84   # PATCH model_info.oicm + blocked
  |        [on change only; 0 writes when steady]
  |
  +-- _maybe_report_health(snapshots, by_uuid, now)   status_persister.py:228
  |     skip if last is not None and (now - last) < health_refresh_seconds (3600)
  |     reports.append({model_name, litellm_model_id, healthy=serving_available,
  |                     error_message, details={**block, cluster}})
  |     if await litellm.report_status(reports):        litellm_client.py:158
  |         for r in reports: _last_health_at[id] = now   # only on success
  |     [on change OR hourly]
  |
  +-- _maybe_heartbeat(snapshots, now)          status_persister.py:270
        skip if no snapshots
        skip if (now - _last_heartbeat) < heartbeat_interval (30s)
        clusters = sorted({snap.cluster})
        results = await litellm.report_heartbeats(clusters)  litellm_client.py:198
        if results: self.checked_at = MappingProxyType(dict(results))
        [every 30s]
```

## 3. Gateway flow (per POST)

```
oicm_status_reports(batch, user_api_key_dict)   oicm_routes.py:106
  from litellm.proxy.proxy_server import prisma_client   # deferred (circular import)
  _require_admin(user_api_key_dict)             oicm_routes.py:92   # 403 unless PROXY_ADMIN
  _require_prisma(prisma_client)                oicm_routes.py:100  # 500 if None
  writes = tuple(
    prisma_client.save_health_check_result(...) # native, utils.py:6429
      -> PrismaClient._clean_details(details)   # utils.py:6419, wraps in prisma.Json
      -> HealthCheckRepository(self).table.create(...)   # INSERT into LiteLLM_HealthCheckTable
    for report in batch.reports
  )
  rows = await asyncio.gather(*writes)
  saved = sum(1 for r in rows if r is not None)  # None = native save failed
  return {"saved": saved, "received": len(batch.reports)}
```

`oicm_heartbeats` (`oicm_routes.py:151`) is identical except each row has
`model_name = f"{_SOURCE_ROW_PREFIX}{hb.cluster}"`, `model_id=None`, `details=None`.

## 4. Native write path (unchanged, reused)

```
PrismaClient.save_health_check_result(model_name, status, healthy_count,
    unhealthy_count, error_message, response_time_ms, details, checked_by, model_id)
  |                                              litellm/proxy/utils.py:6429
  +-- error_message[:500]
  +-- _validate_response_time(response_time_ms)
  +-- _clean_details(details) -> prisma.Json(...)   # the 2026-10-06 fix
  +-- drop None optional fields
  +-- HealthCheckRepository(self).table.create(data=...)   # never raises; None on failure
```

`checked_at` is a Postgres column default (`now()`), so the gateway stamps it.

## 5. Native read path (unchanged, consumed)

```
GET /health/latest    -> get_all_latest_health_checks()  # DISTINCT ON (model_id else model_name)
GET /health/history   -> get_health_check_history(model=, status_filter=, limit=, offset=)
Admin UI HealthCheckComponent  # branches status == "healthy", skips rows with no model id
```

## 6. Data contracts

Request `POST /oicm/v1/status-reports`:
```
{"reporter": "oicm-controller",
 "reports": [{"model_name": str(1..),
              "litellm_model_id": str|None,
              "healthy": bool,
              "error_message": str|None,
              "details": dict|None}]}     # 1..500 entries
```
Response: `{"saved": int, "received": int}`.

Request `POST /oicm/v1/heartbeats`:
```
{"reporter": "oicm-controller",
 "heartbeats": [{"cluster": str(1..)}]}   # 1..500 entries
```
Response: `{"saved": int, "received": int}`.

## 7. Row shapes in LiteLLM_HealthCheckTable

Per-model row: `model_name`=<model>, `model_id`=<uuid>, `status`=healthy/unhealthy,
`details`=<oicm block + cluster>, `checked_by`=oicm-controller, `checked_at`=server.
Source row: `model_name`=oicm-source-<cluster>, `model_id`=null, `status`=healthy,
`details`=null, `checked_at`=server (fresh every 30s).

## 8. Consumers (current state)

| Consumer | Reads | Status |
|---|---|---|
| Admin UI health column | `/health/latest` (via model_id join) | source rows skipped (no model_id) |
| Controller `/status` `source_alive` | its own in-memory `checked_at` | does NOT read source rows |
| openrouter_compat status (Steps 13-18) | not built | the intended consumer of source-row freshness |

## 9. Audit findings (code smell + logic map, 2026-10-07)

### Fixed
| Finding | Layer | Where | Fix |
|---|---|---|---|
| `from typing import cast` unused | L1 | `test_endpoints.py:8` | removed |
| `OicmStatusReport` imported unused | L1 | `test_endpoints.py:20` | removed |
| `import asyncio` inside two handlers, violating sibling convention | L2 | `oicm_routes.py` | moved to top-level (matches `voice_routes.py:12`) |
| Unbounded batch list | L2/L3 | `oicm_routes.py` | added `_MAX_BATCH_SIZE=500` + `max_length=` on both batches |
| `OicmSourceHeartbeat.details` never populated (dead schema) | L2 | `oicm_routes.py:72` | removed field; pass `details=None` |

### Verified clean (no change)
- Controller: pyflakes, vulture, ruff all clean. All removed heartbeat symbols
  have zero residual references. `report_status`/`report_heartbeats` both called.
- Gateway: no dead constants (all 5 have >1 ref). Comments name real symbols.
- Native write path: `None` return handled (counted, not raised); DB-down handled
  (500 via `_require_prisma`); auth handled (403).
- Failure shape tested: `{"saved": 1, "received": 2}`.

### Pre-existing, out of scope
- `test_config.py` 2 failures (stale prod manifest key expectations).
- `ARG001` in `test_litellm_client.py` (4x args/kwargs), `PLR0917` in
  `test_status_persister.py` — both present before this change (verified against
  commit `a90f655ce3`).
- `B008 Depends()` in route defaults — house style (13 uses in `voice_routes.py`).

## 10. Robustness matrix (adversarial inputs)

| Input | Result | Assessment |
|---|---|---|
| empty `reports`/`heartbeats` | 422 | correct (min_length=1) |
| >500 entries | 422 | correct (new cap) |
| missing `healthy` | 422 | correct |
| `healthy` as `"true"` | 200 (coerced) | Pydantic lax bool; acceptable |
| `model_name` = `""` | 422 | correct (min_length=1) |
| `model_name` = `"   "` | 200 | passes validation; harmless (no matching row) |
| `details` as list | 422 | correct (dict_type) |
| unknown extra field | 200 (ignored) | Pydantic default; acceptable |
| `reporter` = `""` | 200 (empty checked_by) | minor; unbounded but non-critical |
| native save returns None | 200 `{"saved":0,...}` | correct (counted, not raised) |
| no auth header | 401/403 | correct |
| non-admin | 403, no writes | correct |
