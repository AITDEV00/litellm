# Dashboard Analytics Reads -> Prisma Engine Bloat -> OOMKills — Logic Map

> **Technique**: [Logic Mapping](../../engineering/techniques/logic_mapping_technique.md) —
> trace-through-before-you-build. Phase 1 (Trace) + Phase 2 (Test) complete;
> live-verified against prod (`adeo-litellm`, 2026-09-15; recurrence
> re-verified 2026-11-26, see section 7). Evidence files in
> [`live-data/README.md`](live-data/README.md).
>
> Companion docs: `retention-flags-and-oom-LOGIC-MAP-v2.md` (09-08 DB-incident
> OOM), `spend-logs-ram-and-model-performance-LOGIC-MAP.md` (write path + known
> slow reads), and `routing-strategy-sync-task-leak-oom-LOGIC-MAP.md` (the
> 2026-09-30 asyncio task-leak OOM cause — pods kept OOMKilling after THIS fix
> shipped because that was a second, independent driver). This map covers the
> **read path that kills pods** — a distinct root cause from the others.

---

## 0. One-paragraph answer

The recurring (~daily) 12Gi OOMKills of `litellm-proxy` are caused by Admin
UI analytics endpoints (`/user/daily/activity/aggregated`,
`/team/daily/activity/aggregated`, `/model/performance` for long windows,
`/global/all_end_users`, and the spend-logs session enrichment) that return
4.8k–27k-row result sets in a **single statement** through Prisma. Prisma's
Rust query engine materializes each full result set as JSON in glibc arenas,
and glibc never returns freed arena memory to the OS — the engine's RSS is a
permanent high-water mark (documented in the official production guide). One
engine on `fgsrb` was measured at **5,061,660 kB Private_Dirty** — every byte
live, none reclaimable. Because engines live inside the same 12Gi cgroup as
the 4 Python workers (~6GiB combined baseline), each dashboard browse session
ratchets the pod toward the limit; the kernel OOM-kills the pod roughly a day
after every restart. No scheduler is involved — the "daily" cadence is the
ratchet period.

---

## 1. The kill chain (Phase 1 map, Phase 2 verified)

```text
Admin UI dashboard browse (811 UI reads / 24h, live-data/09)
    |
    v
ENTRY POINTS (nginx-verified traffic)
    |-- GET /user/daily/activity/aggregated   16 req/24h
    |-- GET /gateway/daily/activity           16 req/24h
    |-- GET /model/performance                28 req/24h
    |-- /ui/* spend / logs / team views       (spend-logs pagination + enrichment)
    |
    v
[common_daily_activity.py]  get_daily_activity_aggregated()   :1252
    |-- _build_aggregated_sql_query()      :655
    |     GROUP BY GROUPING SETS (13 rollup levels) over LiteLLM_DailyUserSpend
    |     WORST CASE: full-range call returns ~27k rows (live-data/08 replay)
    |-- _build_entity_rollup_sql_query()   :750  (companion, 2 more rollup levels)
    |-- asyncio.gather( 2x prisma_client.db.query_raw(...) )  :1324
    |
    |   CALLERS (all funnel into the same builder):
    |   internal_user_endpoints.py:2784  get_user_daily_activity_aggregated
    |   team_endpoints.py:5979           get_team_daily_activity_aggregated
    |   usage_endpoints/ai_usage_chat.py:258  (AI usage chat tool)
    |
    |   SIBLING HEAVY READS (same engine, same effect):
    |   spend_management_endpoints.py:3555  SELECT DISTINCT end_user
    |       FROM LiteLLM_SpendLogs (57GB/15M rows) — avg 7.2s, max 60s/call
    |   spend_management_endpoints.py:4083  session-spend enrichment
    |       GROUP BY over SpendLogs per UI logs page — 1,235 calls, 52k rows
    |   model_performance_endpoints.py:687/872  window>=14d uses
    |       _get_heavy_query_prisma_client() :42 — creates an EXTRA
    |       PrismaClient (timeout=600) = an EXTRA engine subprocess per
    |       worker, created lazily, NEVER terminated
    |
    v
[Prisma client (per granian worker)]  -->  HTTP over localhost
    |
    v
[query-engine subprocess (Rust)]  <-- THE VICTIM
    |-- materializes the FULL result set as JSON in glibc arenas
    |-- glibc frees internally but NEVER returns arena pages to the OS
    |   (official docs: "resident memory behaves as a high-water mark;
    |   glibc does not hand that memory back")
    |
    v
MEMORY RATCHET (live-data/01, /04, /05)
    |-- pod baseline right after restart: ~6GiB
    |     (4 workers x ~1.2GiB python + 4 engines 21-427MiB + prisma sidecars)
    |-- each analytics browse raises the worst engine's high-water mark:
    |     +1,476MiB (09-13 12:40), +1,249MiB (16:30), +2,229MiB (09-14 17:50),
    |     +2,152MiB (09-14 20:30, POST-restart), +1,624MiB (09-15 07:20)
    |-- measured worst engine: VmHWM 5.16GB, 5,061,660 kB Private_Dirty,
    |     13x128MiB + 17x64MiB glibc arenas, ~100% resident (live-data/05)
    |-- the two pods stepped at DIFFERENT times (17:46 vs 17:56 on 09-14):
    |     request-driven, NOT a cron (rules out scheduled restart)
    |
    v
12Gi cgroup limit hit  -->  kernel OOMKill (exit 137)
    |-- 09-13 18:37  (accelerated by an error storm: 4 failed req/s, 60 in-flight)
    |-- 09-14 19:25  (NO error storm: failed req/s ~0.02 — pure ratchet)
    |-- kube-state-metrics confirms last_terminated_reason=OOMKilled both times
    |     (live-data/03)
    |
    v
restart resets to ~6GiB baseline  -->  cycle repeats (~1 day period)
    "feels like a daily auto-restart" — it is the ratchet period, not a scheduler
```

## 2. Where the numbers come from (Phase 2 — live data)

| Step in map | Verification | Artifact |
|---|---|---|
| Pod memory ratchet to 12Gi | Prometheus `container_memory_working_set_bytes` 09-12→09-15, discrete +1-2GiB steps | `live-data/01`, `04` |
| OOMKill (not probe/rollout) | `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"} = 1` both pods; restart counter = 2 each | `live-data/02`, `03` |
| No cron/scheduled restart | No CronJob in cluster touches litellm; pod template unchanged since 09-12 `restartedAt`; restart jumps only at 18:37 (09-13) and 19:25 (09-14) | session evidence |
| Which process holds the memory | `ps` + `/proc/<pid>/smaps_rollup` inside pod: engine pid 615 = 5.06GB Private_Dirty anon; the 4 steady engines 21-427MiB | `live-data/05` |
| Engine is not fragmentation-only | Every 128/64MiB glibc arena segment fully resident (Rss == mapped size) — live retained allocation | `live-data/05` |
| Which SQL floods engines | `pg_stat_statements`: GROUPING SETS aggregate = 310 calls / 1,489,441 rows (4,804/call); DISTINCT end_user = 86 calls / avg 7.2s max 60s | `live-data/06` |
| Table scale driving cost | `LiteLLM_SpendLogs` 57GB / 14.7M rows; `LiteLLM_DailyUserSpend` 34,442 rows; cardinality: 345 api_keys x 273 models x 11 providers x 9 endpoints | `live-data/07` |
| Worst single statement size | Replay of full-range GROUPING SETS = 26,816 rows in ONE statement | `live-data/08` |
| Entry-point traffic | nginx 24h: `/model/performance` 28, `/user/daily/activity/aggregated` 16, `/gateway/daily/activity` 16 + UI spend-logs views | `live-data/09` |
| Deployment shape (4 workers, 12Gi) | `deploy/base/gateway` — `--num_workers 4`, limits 12Gi | `live-data/10` |

## 3. The exact code sections at fault

| # | Section | File:line | What it does wrong |
|---|---|---|---|
| 1 | `_build_aggregated_sql_query` — 13-level GROUPING SETS with **no row cap** | `litellm/proxy/management_endpoints/common_daily_activity.py:655-747` | Returns every rollup level's rows in ONE statement (worst ~27k rows). Correct as SQL; fatal as a Prisma single-statement payload. No LIMIT, no server-side clamp. |
| 2 | `get_daily_activity_aggregated` — executes 2 full queries per request | `common_daily_activity.py:1252-1329` (`asyncio.gather` at :1324) | Both main + entity rollup results are materialized by the engine simultaneously (2x peak). |
| 3 | `global_view_all_end_users` — unbounded DISTINCT over the raw 57GB table | `litellm/proxy/spend_tracking/spend_management_endpoints.py:3534-3562` | `SELECT DISTINCT end_user FROM "LiteLLM_SpendLogs"` with no date bound. avg 7.2s, max 60s per call. |
| 4 | `_view_spend_logs` session enrichment — GROUP BY over raw SpendLogs **on every logs page load** | `spend_management_endpoints.py:4083-4106` (`_count_logs_per_session` call at :4062) | Ships per-session aggregates for every UI logs page; 1,235 calls already. **FIXED 2026-10-13** (`d0b68e2465`): enrichment now bounded to the listing date window so partition pruning applies — see [spend-logs-ui-enrichment-window-fix-LOGIC-MAP.md](spend-logs-ui-enrichment-window-fix-LOGIC-MAP.md). Surfaced as ~14s/page only after session-identity populated `session_id` on every row. |
| 5 | `_get_heavy_query_prisma_client` — lazily created EXTRA engine, never terminated | `litellm/proxy/model_metrics_endpoints/model_performance_endpoints.py:42-87`, used at :687/:872 for windows >= 14d | A dedicated `PrismaClient(timeout=600)` = a second engine subprocess per worker that also ratchets and is never recycled. |
| 6 | Deployment shape: 4 workers x 1 cgroup | `oicm-litellm-layer/deploy/base/gateway` | 4 engines + 4 python heaps in one 12Gi cgroup; official floor is 4Gi **per worker** (16Gi for 4). The analytics engine's ratchet has no headroom. |

## 4. Why it is NOT the earlier hypotheses

| Hypothesis | Disproof |
|---|---|
| "Auto-restart cron" | No CronJob; template annotation unchanged; restart jumps occur only at the two kill timestamps (Prometheus `increase(...restarts_total)` over 4d = exactly 2). |
| "Error-storm traceback amplification" | 09-14 kill: failed-request rate ~0.02 req/s at the time of death (Prometheus). Python-side `mem_monitor` shows workers at 1-2GiB only — the 5GiB lives in a Rust engine, not Python tracebacks. |
| "Memory is page cache / will be reclaimed" | `container_memory_rss` (not cache) hit 11,372-12,129 MiB; engine memory is `Private_Dirty` anon, unreclaimable. |
| "One pod is broken" | Both pods OOMKilled, both engines ratchet; the served-side traffic (UI reads) distributes across both. |

## 5. Root-cause fix directions (what the map says must change — not stopgaps)

1. **Bound what ships through the engine** (sections 3.1-3.4): the GROUPING
   SETS response must be clamped server-side (per-rollup-level row cap or
   aggregate-to-response-shape in SQL so Python receives only the final
   per-date rows it renders). Same for the DISTINCT end_user endpoint (it
   feeds a UI dropdown; a per-key daily rollup table already exists in the
   write path — read it instead of raw logs).
2. **Move analytics reads off the request-path engines** (section 3.5): the
   heavy-query client should be a separate deployment (like the documented
   `LITELLM_JOB_ROLE=worker` topology) or recycled; per-worker lazily-created
   engines that live forever guarantee unbounded ratchet.
3. **Fix the engine allocator's arena behavior** (section 3.6): upstream
   Prisma issue territory — `MALLOC_ARENA_MAX` / allocator tuning for the
   engine subprocess, or an engine restart policy tied to RSS.
4. **Sizing truth** (section 3.6): 12Gi for 4 workers is below the documented
   4Gi/worker floor even before analytics reads; whatever else changes, the
   cgroup shape must match the official floor or the worker count must drop.

## 6. Open questions the map does not yet answer

- Exact arena-growth-per-query coefficient (would need engine-level heap
  profiling; upstream Prisma tracing per the Prisma memory-leak discussions).
- Whether the entity-rollup companion query (:1324 gather) doubles peak or
  pipelines — timing-dependent.

---

## 7. Recurrence on jya0-v1.108.5 (2026-11-26 investigation, live-verified)

The OOM mechanism returned after the v1.108.5 upstream merge. Both prod
replicas OOMKilled on 2026-11-25 (`litellm-proxy-8bd44bf4-dj6jr` 3 restarts,
one container surviving only 49 minutes; `m6fc8` 1 restart), 16Gi limit,
same shape as every prior episode.

### What the live evidence shows

- `m6fc8` sat at 14.4Gi/16Gi with **two extra engines at 4.5GB each**
  (pids 1177, 18638), each the per-worker `_get_heavy_query_prisma_client()`
  engine, each with `prisma_client_queries_total = 1` — **one single query**
  set a ~4.5GB high-water mark. 48 glibc arenas of 64/128MiB, fully
  resident. The steady engines held 111-256MB after 145k-290k queries.
- Prometheus 48h: `m6fc8` climbed 3.4->5.2Gi over 13h, then jumped
  **5.2 -> 14.2Gi in one hour step** (2026-11-25 ~08:20Z) and was killed
  soon after; its replacement jumped to 8.5Gi in the first hour and
  plateaued at 13.8-14.9Gi. `dj6jr` stepped 5.1 -> 8.4Gi the same day.
- Workers themselves were lean (1.0-1.6GB RSS, HWM 1.5-1.8GB): the 09-30
  task-leak fix holds. This recurrence is **pure engine ratchet**.

### Why the 2026-10-29 fix did not prevent it

Commit `6de7d2c9ec1` (2026-10-29) bounded three paths. The upstream v1.108.5
merge (`b46288fadaa`, 2026-11-11) silently dropped two of them in merge
resolution — parent 1 had the fixes, the merged result did not:

1. **EndUserTable full-table read is back**: `resolve_customer_daily_activity_scope`
   (`customer_endpoints.py:525`) again runs `find_many(where={})` with no
   `take`. pg_stat_statements (reset 2026-10-08): 653 calls, **51.5M rows**
   (79k/call, the whole table), 5.6GB scanned. The table has grown to 82,787
   rows (70,653 on 10-29). The dropped constant was
   `_END_USER_ALIAS_SCAN_CAP = 10000`.
2. **`/global/spend/refresh` client leak is back**:
   `spend_management_endpoints.py:3812-3820` again creates a
   `PrismaClient(timeout=6000)` per call with no `disconnect()` (the
   try/finally from the fix is gone).
3. The `LIMIT 50000` row caps on both model-performance heavy paths DID
   survive — and are **insufficient anyway**: the full-range rollup query
   returns only 506 rows (max 656KB/row, ~62MB total `json_agg` payload;
   30d ~16MB). The cap never bites. The payload is amplified ~70x inside
   the engine (serde_json::Value tree, one heap allocation per number
   node; 29.7M histogram bins total in the table), then retained by glibc
   arena high-water-mark. **Bounding rows does not bound bytes.**

Everything below in this section was true at investigation time
(2026-11-26) and is kept as the evidence record; the fixes it asks for
are implemented as of 2026-11-27, listed in the "Fix status" subsection.

### Fix status (implemented 2026-11-27, jya0-v1.108.5 branch)

All four ranked fixes shipped. The drop-detection test module
(`tests/unit/proxy/test_oicm_drop_detection.py`) now pins each one so a
future upstream merge that reverts any of them fails CI instead of
silently OOMing prod again.

1. **Caps restored.** `_END_USER_ALIAS_SCAN_CAP` (10k) on the end-user
   alias scan, `END_USER_FACET_SCAN_CAP` (10k) bound on the
   `global_view_all_end_users` facet query, the `ROW_NUMBER() ...
   level_rank <= 5000` wrapper in `daily_activity_sql.build_aggregated_sql`
   (constants.MAX_ROWS_PER_GROUPING_LEVEL), and
   `global_spend_refresh` once again disconnects its dedicated client in a
   `finally`.
2. **Rollup read bounded by bytes.** `json_agg(ttft_histogram_counts)`
   replaced by `_rollup_array_sum(ttft_histogram_counts)`, a new SQL
   aggregate (migration `20261126120000_add_rollup_array_sum_aggregate`,
   SFUNC = `_rollup_array_add_bigint`) so each row carries one fixed-size
   summed array. Measured on the live prod DB: full-range payload 61.7MB
   -> 142KB (435x smaller). The aggregate was applied to the prod DB
   during verification, so the read path is protected even before the
   next image roll.
3. **Extra-engine pattern stopped.** The module-cached heavy PrismaClient
   singleton is gone; `_heavy_query_prisma_client()` is now a per-call
   `@asynccontextmanager` that connects on entry and disconnects in a
   `finally`, so no query-engine subprocess outlives the request.
4. **Defense-in-depth shipped.** `MALLOC_ARENA_MAX=2`,
   `MALLOC_TRIM_THRESHOLD_=65536`, `MALLOC_MMAP_THRESHOLD_=65536` on the
   gateway container env (engines inherit it; shrinks arena sprawl), plus
   `--granian_workers_lifetime 43200` (12h recycle) in the prod
   deployment command and the matching `--granian_workers_lifetime` CLI
   option. Dev intentionally omits the recycle: debug wants stable PIDs.

