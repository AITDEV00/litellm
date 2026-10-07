# Logic map: OpenRouter-compatible `/endpoints` consumer

Built with `docs/techniques/logic_mapping_technique.md` (Phase 1 trace, Phase 2
verify against live data, Phase 4 re-verify) and audited with
`docs/techniques/code_smell_detection_technique.md` (L1-L4).

Scope: the consumer side of the OICM status work, i.e. the
`litellm/proxy/openrouter_compat/` package as of commit `8e8b74e4b1`, plus the
two commits before it. The producer side (controller polling, the native health
table writes) is mapped separately in
`docs/oicm-status/LOGIC-MAP-2026-10-07-health-table.md`; this document picks up
where that one ends and replaces its §8 ("openrouter_compat status ... not
built").

Every function in the flow with file:line refs, inputs/outputs, and side
effects. Line numbers are from the commit named above.

## 1. Entry points

| Entry | Where | Auth |
|---|---|---|
| `GET /api/v1/models` | `routes/models.py:46` `list_models` | `user_api_key_auth` |
| `GET /api/v1/models/{author}/{slug}/endpoints` | `routes/models.py:71` `model_endpoints` | `user_api_key_auth` |
| `GET /api/v1/models/{slug}/endpoints` | `routes/models.py:91` `model_endpoints_bare_slug` | `user_api_key_auth` |

All three are mounted in `proxy_server.py:18888` (`app.include_router`) and
declared in `LiteLLMRoutes.openai_routes` (`litellm/proxy/_types.py:396-400`).
That list is consumed in two places, so a route missing from it is silently
absent from both the auth surface and the custom OpenAPI spec:

- `proxy_server.py:1634` `custom_openapi` filters the published spec to it.
- `auth/auth_checks.py:329` builds `all_routes` from it.

Both endpoint routes delegate to one helper, `_endpoints_response`
(`routes/models.py:115`). The bare route passes `author="litellm"`
(`_CANONICAL_NAMESPACE`, `routes/models.py:23`).

## 2. Request flow

```
GET /api/v1/models/{author}/{slug}/endpoints
  |
  +-- user_api_key_auth (FastAPI dependency)           # 401 without a key
  |
  +-- model_endpoints(author, slug, ...)               routes/models.py:71
  |     or model_endpoints_bare_slug(slug, ...)        routes/models.py:91
  |       -> _endpoints_response(request, author, slug, ...)   routes/models.py:115
  |
  +-- _get_service(request)                            routes/models.py:26
  |     app.state.openrouter_service cached? -> return it
  |     else: lazy import proxy_server, read llm_router
  |           500 if llm_router is None
  |           build OpenRouterModelsService(llm_router, details_base_url=base_url)
  |           store on app.state
  |
  +-- OpenRouterModelsService.get_model_endpoints(...)  models_service.py:94
  |     |
  |     +-- _resolve_and_discover(...)                  models_service.py:186
  |     |     |
  |     |     +-- DeploymentResolver.resolve_for_request  discovery/resolver.py:46
  |     |     |     get_available_models_for_user(...)   # visibility gate
  |     |     |     for deployment in _iter_deployments():   resolver.py:90
  |     |     |       skip if model_name not in accessible
  |     |     |       _build_descriptor(...)             resolver.py:101
  |     |     |         logical_model_name = deployment.model_name   <-- the match key
  |     |     |         deployment_id = model_info.id or logical_model_name
  |     |     |       skip if duplicate (api_base, provider)
  |     |     |
  |     |     +-- DiscoveryService.discover_many(...)    service.py:41
  |     |           _deduplicate_targets(...)            service.py:102
  |     |           asyncio.gather over adapters, bounded concurrency
  |     |           DiscoveryAdapterRegistry.resolve     discovery/registry.py:42
  |     |             _detect_runtime_kind               discovery/registry.py:54
  |     |           InMemoryDiscoveryCache (ttl 15s)     cache/memory.py:24
  |     |           -> DiscoveryResult(discoveries, failed_logical_models)
  |     |     |
  |     |     +-- ModelAggregator.aggregate_all(...)     aggregation/aggregator.py:28
  |     |           -> list[AggregatedModel], set[str] failed
  |     |
  |     +-- public_id = mapper.canonical_id(f"{author}/{slug}")   models_service.py:113
  |     |
  |     +-- _find_model(aggregated, author, slug)       models_service.py:159
  |     |     requested = canonical_id("author/slug")
  |     |     match: canonical_id(m.logical_model_name) == requested
  |     |     -> None if no match
  |     |
  |     +-- if model is None:
  |     |     _is_known_undiscovered(failed, public_id) models_service.py:137
  |     |       public_id in {canonical_id(n) for n in failed}
  |     |     -> map_empty(public_id, logical_model_name=slug)   mapping/endpoints.py:87
  |     |     -> or None -> HTTPException 404
  |     |
  |     +-- page = model.model_copy(deployments[offset:offset+limit])
  |     +-- CapabilityEnricher.enrich                     enrichment/capabilities.py:20
  |     +-- LiteLLMMetadataEnricher.enrich               enrichment/litellm_metadata.py:56
  |     |
  |     +-- _resolve_statuses(enriched, prisma_client)  models_service.py:144
  |     |     GatewayStatusReader(prisma).read(...)      status_reader.py:37
  |     |       (see section 3)
  |     |     GatewayStateResolver.resolve(...) per deployment  gateway_status.py:135
  |     |     -> Mapping[deployment_id, GatewayStatus]
  |     |
  |     +-- OpenRouterEndpointsMapper.map_endpoints(...) mapping/endpoints.py:62
  |           _to_endpoint per deployment             mapping/endpoints.py:106
  |             pricing: PricingResolver.resolve_for_deployments   enrichment/pricing.py:38
  |             status: GatewayStatus.endpoint_status()  gateway_status.py:95
  |           ListEndpointsResponse.model_validate + per-endpoint dump
  |
  +-- HTTP 200 {"data": {...}}   or   HTTP 404 {"detail": "Model not found"}
```

## 3. Health read path (the status source)

```
GatewayStatusReader.read(model_name, deployment_ids)      status_reader.py:37
  if prisma is None or not deployment_ids: return {}      # no DB, no status
  |
  +-- fetch_latest_health_checks_for_models(prisma, [model_name])
  |     db/health_check_latest.py:86
  |     SQL: SELECT DISTINCT ON (model_id, model_name) ...
  |          WHERE model_name = ANY($1) ORDER BY checked_at DESC
  |     ^ filters on model_name, NOT model_id
  |     by_id = {row.model_id: row for row in rows if row.model_id}
  |
  +-- _read_heartbeats()                                  status_reader.py:60
  |     fetch_latest_health_checks(prisma)   # all rows, no filter
  |     keep rows where model_id is None and
  |       model_name.startswith("oicm-source-")           # _SOURCE_ROW_PREFIX
  |     key by cluster name (prefix stripped)
  |     on any exception: log, return {}   # noqa: BLE001, decoration must not 500
  |
  +-- per deployment_id:
        _inputs(row, heartbeats)                          status_reader.py:76
          details = row.details if dict else {}
          cluster = details["cluster"]
          heartbeat = heartbeats.get(cluster)             # freshness source
          replicas = details["replicas"] -> desired/available
          -> GatewayStatusInputs(
               oicm_status=details["status"],
               serving_available=details["serving_available"],
               replicas_desired, replicas_available,
               observed_at=details["observed_at"],
               cluster=cluster,
               health_status=row.status,
               source_checked_at=heartbeat.checked_at)
```

The `details` keys are written by the controller's `build_block`
(`controller/status_persister.py:60`), with `cluster` merged in at
`controller/status_persister.py:258`. Verified against a live row (section 8).

## 4. Verdict resolution

```
GatewayStateResolver.resolve(inputs)                      gateway_status.py:135
  checked_at = inputs.source_checked_at                   # heartbeat, not model row
  stale      = _is_stale(checked_at)                      gateway_status.py:152
                 None -> True; age > 90s -> True
  lifecycle  = _lifecycle(inputs.oicm_status)             gateway_status.py:163
                 Ready|Available        -> stable
                 Deploying|Pending      -> deploying
                 Stopped|Undeploying    -> stopped
                 Failed                 -> failed
                 else                   -> unknown
  availability = "unknown" if stale
                 else _availability(oicm_status, serving_available)  gateway_status.py:175
                   stopped|failed|deploying -> offline
                   Ready|Available -> _AVAILABILITY_BY_SERVING[serving]
                   else -> unknown
  -> GatewayStatus(...)                                   gateway_status.py:82
```

`GatewayStatus.endpoint_status()` (`gateway_status.py:95`) then maps that verdict
onto OpenRouter's numeric enum, via two declarative tables
(`gateway_status.py:64`, `:69`):

```
stale -> None (omit)
else _ENDPOINT_STATUS_BY_LIFECYCLE.get(lifecycle)     # failed/stopped/deploying
     or _ENDPOINT_STATUS_BY_AVAILABILITY.get(availability)  # degraded/online
     or None
```

Lifecycle deliberately wins over availability, and no lifecycle maps to `0`, so
a table miss is unambiguous. `-1` is unassigned on purpose (reserved for a
load-based signal; see `docs/oicm-status/FEASIBILITY-ANSWERS.md`).

## 5. Response shape

```
ListEndpointsResponse(
  id           = public_id            # canonical, e.g. litellm/hamsa-tts
  name         = display_name or upstream_model_id or public_id
  created      = identity.created or 0
  description  = "<public_id> served by the gateway across N deployment(s)."
  architecture = all-null/empty       # we observe none of it
  endpoints    = [GatewayEndpoint per deployment]
)
```

`GatewayEndpoint` (`mapping/endpoints.py:41`) subclasses the official
`PublicEndpoint` and adds `gateway_status`. The extra field survives only
because each endpoint is serialized on its own
(`mapping/endpoints.py:84`); nesting the subclass in the declared
`list[PublicEndpoint]` would drop it. That is the single most fragile line in
the flow and it is why `map_endpoints` does a second per-endpoint dump.

Fields we compute vs. leave honestly empty:

| Computed | Source |
|---|---|
| `model_id` | `AggregatedModel.logical_model_name` (routing name) |
| `model_name` | `deployment.identity.upstream_model_id` (served id) |
| `name`, `tag`, `provider_name` | runtime kind from discovery |
| `context_length`, `max_prompt_tokens`, `max_completion_tokens` | deployment limits |
| `pricing.prompt` / `.completion` | litellm registry |
| `status` | section 4 |
| `gateway_status` | sections 3-4 |

Left null/empty on purpose: `latency_last_30m`, `throughput_last_30m`,
`uptime_last_5m/30m/1d`, `quantization`, `supported_parameters`,
`supports_tool_choice` (all-false), `native_tools`, the three `supports_*`
booleans. These are nullable fields, so `None` serializes as `null`, not
omitted. `status` is the one optional field, and omitting it is how we say
"no opinion".

## 6. Match key

`{author}/{slug}` matches the registered LiteLLM `model_name`, the string a
caller puts in the request body. Both sides are canonicalized through
`OpenRouterModelMapper.canonical_id` (`mapping/openrouter.py:120`), which
prefixes the `litellm` namespace onto a bare id. Consequences:

- `Qwen/Qwen3.6-35B-A3B-FP8` -> canonical `Qwen/Qwen3.6-35B-A3B-FP8`
- `hamsa-tts` -> canonical `litellm/hamsa-tts`, reachable as either URL form
- `wrong/hamsa-tts` -> canonical `wrong/hamsa-tts` -> no match -> 404

It is deliberately not the upstream served id. Live proof they differ, and that
LiteLLM rejects the served name, is in design §31. `model_name` is also not
unique (several clusters can share one), so one URL can return several
endpoints.

## 7. Consumers of the output

| Consumer | Reads | Status |
|---|---|---|
| OpenRouter-compatible clients | `/api/v1/models`, both `/endpoints` forms | live |
| Admin UI | `/health/latest` via `model_id` | unchanged, source rows skipped |
| OICM controller | its own in-memory `checked_at` | does NOT read source rows |
| `gateway_status` on each endpoint | source-row freshness via `checked_at` | the intended consumer of source rows |

## 8. Live verification (2026-10-07, dev)

Scraped, not assumed:

- All 28 models: 28/28 canonical `/endpoints` URLs return HTTP 200; the 11 bare
  ids return HTTP 200 with byte-identical bodies; 22 endpoints carry `status=0`;
  6 return empty-but-valid `[]`.
- Wrong author: 404 for both a discovered and an undiscovered bare id.
- Unauthenticated: 401. Unknown model: 404.
- One live `details` blob confirmed every key `status_reader._inputs` reads:
  `cluster`, `status`, `serving_available`, `replicas.desired/available`,
  `observed_at` (plus `v`, `gateway_uuid`, `status_changed_at`, `error_msg`,
  which this consumer ignores).
- Observed `status` values in the table: `Ready` (505 rows), `Deploying` (2).
  The resolver maps all seven OICM statuses, so `Available`, `Pending`,
  `Stopped`, `Failed`, `Undeploying` are covered but unobserved in dev.
- Prod pods untouched (`started=2026-09-30`).

## 9. Audit findings (code smell + logic map, 2026-10-07)

### Fixed

| Finding | Layer | Where | Fix |
|---|---|---|---|
| `PLR0911` 7 returns in one function | L1 | `gateway_status.py:82` | Replaced the 6-branch if-chain with two declarative tables (`_ENDPOINT_STATUS_BY_LIFECYCLE`, `_ENDPOINT_STATUS_BY_AVAILABILITY`) and two lookups. Also makes the lifecycle-over-availability precedence explicit. |
| `B010` `setattr` with a constant name | L1 | `routes/models.py:41` | `setattr(app_state, "openrouter_service", built)` -> `app_state.openrouter_service = built`. |

### Verified clean (no change)

- pyflakes and vulture (`--min-confidence 80`) both silent on the package.
- All 5 `_ENDPOINT_STATUS_*` constants referenced from the tables, so none is dead.
- All 9 `GatewayStatus` fields written at `gateway_status.py:141-149`; all 8
  `GatewayStatusInputs` fields read by `resolve`/its helpers.
- Every OICM `DeploymentStatus` member (`controller/status/snapshot.py:15`) maps
  to a defined lifecycle; no branch falls through to `unknown` for a real status.
- Every `status` value reachable from the tables is a member of the SDK's
  `EndpointStatus` union; `-1` is reachable from neither table (reserved).
- All 14 `design §N` references in the package resolve to a real section.
- The producer/consumer `details` contract matches, verified against a live row.

### Recorded as deliberate

| Finding | Where | Why it stays |
|---|---|---|
| `B008` `Depends()` in argument defaults (3x) | `routes/models.py:48,75,94` | House style for FastAPI routes; 13 identical uses in `voice_routes.py`. Not a defect. |
| `PLR0917` 6 positional args | `routes/models.py:71` | FastAPI path/query params plus the auth dependency; collapsing them would obscure the route signature. |
| 2 basedpyright errors | `enrichment/pricing.py:66`, `mapping/openrouter.py:67` | Pre-existing before this work; identical count at `bb796e8c2a~2`. |
| `gateway_status` not readable via the declared schema | `mapping/endpoints.py:41` | Intentional extension to the OpenRouter contract; see §5. |

### L4 recheck

Re-ran all three L1 tools after the fixes. No cascades: the two edits removed a
finding each and introduced none. 118 `openrouter_compat` tests pass, unchanged
from before the refactor, so the table rewrite preserved behavior.
