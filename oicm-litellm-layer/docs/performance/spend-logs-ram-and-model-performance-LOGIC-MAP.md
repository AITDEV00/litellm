# Spend Logs + RAM + Model Performance — Logic Map

Phase 1 (Trace) applied to the log write path, its RAM footprint, and the
Model Performance tab's role in it. Phase 2 (Test) values verified against
live prod (mlops, 2026-09-08) inline as `LIVE:` markers. Companion to
`model-performance-30d-read-fix-LOGIC-MAP.md` (read path) — this doc covers
the write path and memory.

---

## 0. One-paragraph answer

The Model Performance tab did NOT add DB log volume. It added one in-RAM
transaction object per request (a 1-minute rollup bucket), drained by a
scheduler every ~23s and upserted incrementally into
`LiteLLM_ModelPerformanceRollup` — the 48GB raw table is never scanned to
build it. Log volume is driven entirely by `LiteLLM_SpendLogs` row inserts
(prompt/response size controls that). The RAM pressure in the proxy comes
from bounded-but-large buffers (64MB spend-log queue + 9 × 1000-slot queues)
plus unbounded concurrency × request/response body buffering; the OOMKills
were spikes of the latter. The remaining REAL issue on the tab is the
entity-scoped read: with any team/user/key filter it scans the raw 48GB
table 3× per request — retention (30d) both fixes the disk and shrinks that
scan.

## 1. Write path — entry to Postgres

```
LLM response completes
    |
    v
Logging.async_success_handler            litellm/litellm_core_utils/litellm_logging.py:2680 (batch :2926/:2940 streaming)
    |
    v
ProxyDBLogger.async_log_success_event    litellm/proxy/hooks/proxy_track_cost_callback.py:62
    |  -> _PROXY_track_cost_callback     :211
    v
DBSpendUpdateWriter.update_database      litellm/proxy/db/db_spend_update_writer.py:251
    |
    |-- get_logging_payload              spend_tracking_utils.py:251  (builds the SpendLogs ROW in RAM)
    |     |-- response / proxy_server_request  (FULL bodies if store_prompts_in_spend_logs, truncated 2048/string :710)
    |     |-- metadata                        _types.py:3510 (may DUPLICATE request body inside metadata :3533)
    |
    |-- enqueue_spend_logs               litellm/proxy/utils.py:5978
    |     --> prisma_client.spend_log_transactions   (plain list, utils.py:3344)
    |         BOUNDED: 64MB byte budget (SPEND_LOG_QUEUE_MAX_BYTES, constants.py:1546)
    |         eviction: drops OLDEST, keeps newest  (spend_log_batching.py:70-96)
    |
    |-- _batch_database_updates (async task)  db_spend_update_writer.py:332-344
    |     --> 9 asyncio.Queue(maxsize=1000)  (constants.py:290):
    |         spend_update_queue + 6 daily_* queues + model_performance_rollup_update_queue
    |         + tool_discovery_queue
    |         at 80% full -> self-AGGREGATE by entity (spend_update_queue.py:38-46)
    |         at 100% full -> await put() BLOCKS THE REQUEST PATH (backpressure, not OOM)
    |
    v  (scheduler drains)
update_spend job every proxy_batch_write_at+rand(0..5)s   proxy_server.py:8916 (default 10s, LIVE config sets 1)
update_model_performance_rollup_job every interval x2.3   proxy_server.py:8984 (constants.py:1597)
spend-log queue monitor polls 2s, drains at >=100 rows    utils.py:6429 (constants.py:1545/1547)
    |
    |-- use_redis_transaction_buffer=true (LIVE: on)
    |     store_in_memory_spend_updates_in_redis   redis_update_buffer.py:133
    |       RPUSH one JSON per queue -> 7 Redis keys (constants.py:280-287)
    |       + litellm_model_performance_rollup_update_buffer (:752)
    |     leader pod: PodLockManager (Redis SET NX EX 60s, pod_lock_manager.py:47)
    |       LPOP 100/tick/queue (MAX_REDIS_BUFFER_DEQUEUE_COUNT, constants.py:288)
    |       -> _commit_spend_updates_to_db   db_spend_update_writer.py:1409
    |
    v
update_spend_logs_job                    utils.py:6301
    |-- dequeue_spend_logs(10_000)       utils.py:6010
    |-- ProxyUpdateSpend.update_spend_logs :6066 (batches of 1000)
    |     split to statements <=2MB and <=100 rows   spend_log_batching.py:118
    |     poison-row bisect              utils.py:6502
    |-- SpendLogsRepository.create_many(skip_duplicates=True)   repositories/table_repositories.py:65
          -> INSERT ... ON CONFLICT DO NOTHING      (PK=request_id; cache hits suffixed :448)
```

`LIVE` verification: spend table 48GB / 13.2M rows; Redis buffer keys exist
(7,633 keys, ALL with TTL, 123MB of 512MB); proxy steady 6.7Gi/8Gi.

## 2. Where the RAM goes (and why 8Gi is close)

| Consumer | Bound | File:line | Risk |
|---|---|---|---|
| `spend_log_transactions` | 64MB byte budget, evicts oldest | utils.py:5983-6006 | Sustained DB outage silently DROPS rows (bounded, not growing) |
| 9 × asyncio.Queue | 1000 items each; aggregate@80% | spend_update_queue.py:19, daily_spend_update_queue.py:47 | Full queue BLOCKS request path (backpressure) |
| `tool_usage_transactions` / `autorouter_turn_transactions` | UNBOUNDED list, drained 10k/tick | db_spend_update_writer.py:392-394, utils.py:6358 | Grows if monitor dies/falls behind |
| Redis buffer lists | UNBOUNDED RPUSH; drain 100/tick | redis_update_buffer.py:262, :420 | Grows if no pod acquires lock (DB outage) |
| Per-request payload build | ~KB-MB transient per request | spend_tracking_utils.py:459-505 | Concurrency × body size — the OOM driver |
| Rollup txn per request | 1-min bucket + 32-bin histogram | db_spend_update_writer.py:2315 | Negligible; merged in queue |

The OOMKills (Sep 5/7, exit 137, 8Gi limit) are spikes of per-request
buffering under concurrency, NOT queue growth — every queue here is bounded.
The tab's rollup adds ~a KB-scale object per request; it is not the problem.

## 3. How the Model Performance tab's data is written (the trace requested)

Per request, ONE incremental transaction (no table scans, ever):

```
add_spend_log_transaction_to_model_performance_rollup   db_spend_update_writer.py:2315
  - skip cache hits (:2330)
  - 1-minute bucket keyed (model_group, bucket_start)
  - fields: request_count, sums, MIN/MAX ttft, 32-bin TTFT histogram, starts/ends
    (cross-minute request emits a second count=0 end-bucket txn)
  - ModelPerformanceRollupUpdateQueue.add_update        model_performance_rollup_update_queue.py:133
       |
       v  every batch_writing_interval x 2.3
store_in_memory_model_performance_rollup_updates_in_redis  redis_update_buffer.py:752
       |  (merged by bucket key before push)
       v  leader pod lpop <=100
update_model_performance_rollup                         db_spend_update_writer.py:1979 (BATCH_SIZE=200)
  - _execute_rollup_upsert :138-204
    INSERT ... ON CONFLICT ("model_group","bucket_start") DO UPDATE
    monoid: sums add, LEAST/GREATEST for min/max, array-add for histogram
```

`LIVE`: `LiteLLM_ModelPerformanceRollup` = 266MB total (vs 48GB raw) —
one row per model_group+minute. There is NO job that recomputes from
`LiteLLM_SpendLogs`; the only raw scan is the one-time paginated backfill
script `db_scripts/backfill_model_performance_rollup.py` (5000/batch).

## 4. Read path — where the remaining issue is

| View | Table | Status |
|---|---|---|
| Global, any range (30d/MTD/YTD) | `LiteLLM_ModelPerformanceRollup` — single SQL, coarse-bucket CTE | FIXED (commit 7e410cffa3, 30d ~16s -> ~5s) |
| Prometheus windows 1m-1h, global | Prometheus, no DB | OK |
| ANY entity filter (team/user/key/end_user/agent) | **raw `LiteLLM_SpendLogs`** 3 scans + PERCENTILE_CONT + window fn | **OPEN — minutes on 48GB; heavy client 600s masks it** |

Endpoints still aggregating raw logs: `get_global_activity` (:489),
`get_global_spend_report` (:1166), `ui_view_spend_logs` (:2188, paginated)
in `litellm/proxy/spend_tracking/spend_management_endpoints.py`.

Doc drift found: LOGIC-MAP claims custom ranges have cache TTL 0 (always
fresh); actual code derives `window="7d"` for >=48h so they get the 300s TTL
(`_DB_CACHE_TTL`, model_performance_endpoints.py:81-88). Harmless (exact
timestamp key) but the claim is stale.

## 5. Issue list (ranked)

1. **No retention on `LiteLLM_SpendLogs`** — root of the disk-full incident.
   Fix: `maximum_spend_logs_retention_period: "30d"` (+ `disable_error_logs`).
   Side benefit: entity-scoped model-performance reads scan 4x less raw data
   after backlog cleanup (75d -> 30d window).
2. **Entity-scoped performance reads hit raw logs 3x** — the only remaining
   slow path on the tab. Long-term: per-entity daily rollup or accept slower
   reads; short-term: retention shrinks the scan surface.
3. **Proxy memory headroom** — 6.7Gi steady / 8Gi limit, OOMKills on spikes.
   The log pipeline's queues are bounded; the driver is concurrent
   request/response buffering. Raise limit to 12Gi / add replicas.
4. **Unbounded buffers worth knowing, not urgent**: tool/autorouter lists
   (RAM), Redis buffer lists (Redis RAM + 512MB LRU evicts oldest under
   pressure), data-loss windows in redis restore paths
   (redis_update_buffer.py:395-402, db_spend_update_writer.py:1315-1322).

## 6. Phase 2 verification performed

Live scrapes used to validate this map (values cited inline above): table
sizes via `pg_statio_user_tables`, Redis `INFO memory/persistence/keyspace`
+ `--scan` sample, proxy `kubectl top`, incident postmortem table sizes.
End-to-end request tracing not performed (would need a canary request); the
write chain is corroborated by code inspection only — mark as unverified
steps if debugging a specific log row.
