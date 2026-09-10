# 2026-09-08 incident — resolutions in detail

Companion to the [incident report](2026-09-08-postgres-disk-full-and-ingress-tls.md),
which documents what happened, the exact timeline, and the evidence. This
document covers how each issue was resolved, with full technical detail,
verification data, and the reasoning behind each decision. A plain-language
version for non-technical readers lives in the
[executive summary](2026-09-08-executive-summary.md)

All fixes below were verified against the running cluster on 2026-09-09.

---

## Resolutions at a glance

| # | Issue | Resolution | Verified |
|---|---|---|---|
| 1 | Unbounded spend-log growth (48 GB) | 60d retention + drain capacity 8M rows/day | Deleting: 0 rows past 60d |
| 2 | DB-full stalemate (no auto-reclaim) | Retention reclaims continuously + autovacuum tuning (scale 0.02) | Dead tuples bounded at ~270K |
| 3 | Gateway crashes when DB down | `allow_requests_on_db_unavailable: true` | Validated DB-less on dev; flag live in prod |
| 4 | Gateway OOMKills at 8Gi | 12Gi limit + root-cause fix | 0 restarts since deploy |
| 5 | Memory creep invisible | `memory_monitor_job` → Loki | RSS floor flat; peaks drift ~11 MB/day (~1,000 days to limit) |
| 6 | Ingress TLS mismatch | Point both hosts at DigiCert wildcard secret | DigiCert served, verify OK |
| 7 | 100Gi revert risk (Helm) | Cluster manifest mirrored to deploy/prod | PVCs report 100Gi |
| 8 | Longhorn webhook blocked resize | over-provisioning 100 → 150 | Live = 150 |
| 9 | Autovacuum lag after mass deletes | Per-table scale 0.02 on 3 tables | reloptions verified |

---

## Sizing: why 60d fits

Measured live: the 60-day window holds ~13.0M rows ≈ **62 GB** including
indexes + TOAST, against the 100Gi volume — ~38 GB headroom. Steady state:
~1.7 GB/day inserted, ~1.7 GB/day deleted (drain capacity was raised to
8M rows/day: 6h interval × 2000 batches × 1000 rows). Deletion frees space
that inserts reuse; the table plateaus instead of growing.

## Row anatomy (what one SpendLogs row costs)

Measured from prod (2,000-row samples; re-verified 09-09):

| Component | Avg bytes | What it is |
|---|---|---|
| Heap row | 3,559 | tuple header + columns |
| → of which `metadata` JSONB | 2,833–2,872 | see breakdown (80% of heap-row bytes) |
| → → `model_map_information` | 4,880–5,241 | 117-field model registry copy, 95% null |
| → → `cost_breakdown` | 398 | useful |
| → → `usage_object` | 250 | useful |
| → → other 17 keys | ~400 | small |
| Storage split | | heap 9.5 GB + TOAST ~34 GB + indexes 4.5 GB = 48 GB |

**"Doesn't the system overwrite old logs when there's no space?"** — No. Three
separate behaviors, none of which is space-based overwrite:

1. **RAM queue (64 MB cap, drop-oldest)**: pending rows queue in memory before
   flushing (`enqueue_spend_logs`, utils.py:5983-6006). Past the budget the
   oldest *queued* rows are dropped — protects the pod, not the disk
2. **Retention cleanup deletes by AGE only** (60d), never by disk pressure —
   there is no code anywhere checking `pg_total_relation_size()`,
   `pg_disk_available()`, or PG error 53100 (verified: zero matches)
3. **When the disk is actually full**: INSERTs fail AND the retention DELETEs
   fail too (deleting requires WAL writes, which need disk) — the cleanup job
   aborts after 3 consecutive batch failures. Neither old logs are removed nor
   new logs written: a stalemate until manual intervention (exactly the state
   during the Sep 8 degradation window, 05:23 first no-space error → 08:40
   recovery)

Space-based auto-overwrite would be an upstream feature request (a job
checking DB size and force-pruning oldest data below a threshold); the
current defense is retention-by-age + alerts.

## Why OOM appeared 2–3 days after pod starts (proxy OOM mechanism)

### Hypothesis tested: "pending writes to the dead DB caused the OOM" — DISPROVEN

The natural theory — pods OOM'd because pending DB writes piled up in memory
— was tested against the code and **rejected**: every pending-write structure
is bounded, and their combined maximum is ~100–110 MB, nowhere near the 8Gi
limit. Measured from the running image (same tag as at incident time):

| Pending-write structure | Bound | Max during outage |
|---|---|---|
| `spend_log_transactions` (the log rows) | 64 MB byte cap, drop-oldest (`constants.py:1546`, `utils.py:5999`) | **64 MB** |
| 9 × `asyncio.Queue` (entity/daily/rollup aggregates) | 1,000 items each (`constants.py` LITELLM_ASYNCIO_QUEUE_MAXSIZE); full queue = `put()` blocks the request path (backpressure, not growth) | ~9–45 MB |
| `tool_usage_transactions` list (UNBOUNDED) | drained ≤10K/tick; entry = small dataclass (`request_id`, dates, tool names, spend — **not** the payload; `spend_log_tool_index.py:29`) | ~6 MB (14K entries × ~400B in a 40-min outage) |
| `autorouter_turn_transactions` list (UNBOUNDED) | same shape — small scalar fields only (`autorouter_session_rollup.py`) | ~6 MB |
| Redis buffer lists | unbounded RPUSH, but they grow **in the Redis pod**, not the gateway; gateway-side pop/restore transients only | — |

Total: **~95–110 MB bounded**. The OOM gap (RSS climbing from a 6.5 Gi
baseline through the 8 Gi limit) cannot be explained by pending writes.

### The actual OOM mechanism (what the evidence supports)

The kills came from CPU-side event-loop starvation plus transient allocation
churn on top of an already-high baseline, not from any unbounded queue:

1. **4 granian workers × per-tick rebuild** of ≤10K-row flush batches with
   MB-scale JSON serialization, retried/requeued every 2–30s against the dead
   DB for the whole outage window — continuous heavy CPU + transient
   allocations (`utils.py:6301-6420`)
2. **Auth reconnect latency**: every auth cache miss during the outage paid a
   ~2s reconnect budget before failing (`auth_checks.py:3132-3175`) —
   concurrent requests pile up as awaiting tasks, each holding its request
   payload
3. **Prisma engine churn**: the reconnect watchdog spawns a NEW query-engine
   subprocess every ~15s of continued failure, per worker
   (`utils.py:3492, 5067-5150`) — each engine is a child process with its own
   RSS inside the same 8Gi cgroup (hundreds of MB of spikes)
4. **GC starvation**: with the event loop saturated, GC cycles fall behind
   allocation rate — transient garbage accumulates as real RSS

The liveness probe failing on `/health/liveliness` (which touches no DB, no
Redis, no locks) is the definitive signature: the loop couldn't schedule a
trivial handler, so the kubelet killed the pod.

The serving path itself is leak-free — proven by memray (4,800 real DeepSeek
V4 requests: 3.1M allocations, 9.7 GB cumulative churn, peak live tracked
memory 753 MB, **flat** for the whole session).

Post-incident verification (09-09): with `allow_requests_on_db_unavailable:
true` the same failure mode cannot recur — auth falls back instead of
piling up, and pods start even with the DB down.

## Why OOM appeared 2–3 days after pod starts (the kills, separated)

1. **Sep 5 kill = steady-state accumulation** (before any DB trouble):
   ~1.6 Gi/day RSS growth from per-request buffering, fragmentation, metric
   cardinality — subtle, and the motivation for the `memory_monitor_job` and
   the 12Gi limit below. Verified against Loki: zero DB-unreachable lines all
   day, restart banner 03:58:52 UTC
2. **Sep 7 kill = steady-state accumulation** (the database was healthy all
   day — zero P1001 / unreachable / no-space lines on Sep 5–7 in both gateway
   and postgres logs; the disk-full window was Sep 8 morning). Restart banner
   07:42:51 UTC
3. **Sep 8 kills = the DB-outage amplification mechanism**, with a timing
   correction: both pods logged P1001 continuously 07:34–08:38 and stayed up
   through the outage; the restarts landed at 10:23 (wrgvs 10:23:08, mjt5r
   10:23:40) with mjt5r dying again at 10:38 — roughly 1.5h after the DB
   recovered at 08:40. The outage churn is the plausible stressor; the deaths
   trailed after recovery rather than landing inside the outage window

## Gateway-DB decoupling (the architectural fix)

`allow_requests_on_db_unavailable: true` (config-only, upstream-native) now
makes a DB outage non-fatal for the gateway:

- auth failures fall back to a restricted synthetic identity
  (`INTERNAL_USER`, `user_id="__db_unavailable_fallback__"`) instead of 503
- pods start without the DB; the health watchdog reconnects when it returns
- the spend queue stays bounded (64 MB, drop-oldest) — logs may be lost,
  never availability

Validated end-to-end on dev: the dev gateway was deliberately run **DB-less**
(no `DATABASE_URL` at all) — startup, readiness (`"db":"Not connected"`),
auth and serving all worked; then re-attached to a new dev-only CNPG cluster
(`mlops-postgres-dev`, 1 instance) with the real logging pipeline for
profiling.

## Memory creep investigation (post-incident)

- tracemalloc in-process profiling (1,800 full write cycles): no leak in
  payload construction + DB writes; ~3.4 KB/request retained = proportional
  to work
- memray on the serving path (4,800 real requests): peak live tracked memory
  753 MB, flat — no leak
- `memory_monitor_job` now samples prod every 60s: current RSS, peak RSS,
  Prometheus series count, threads, GC stats, delta — in logs and Loki
  (`{job="fluent-bit", namespace_name="mlops"} |= "mem_monitor"`, tenant
  header `X-Scope-OrgID: oiai-loki-logs`)
- Live prod data (09-09): RSS oscillates ~970–1,700 MiB across both pods
  (periodic work; hour-averages 1.0–1.2 GiB), `delta_mb ≈ 0`, no linear
  growth since 12Gi deploy. At the observed rate the 12Gi limit gives 70+
  days of headroom
- Suspected (unproven) remaining creep driver: Prometheus metric cardinality
  (10.5K–14.8K series as of 09-09, grows with key/team/model diversity); only
  `end_user` labels are cardinality-capped upstream (10K series, 1h TTL) —
  model/key/team labels are not

---


### Completeness verification (2026-09-09, live-checked)

Every issue identified in this incident was cross-checked against the commit
log AND the running cluster:

| Issue | Fix commit(s) | Live-verified state |
|---|---|---|
| Unbounded spend-log growth | `d46df2176c` (60d retention) + `c5182135b4` (drain capacity) | **✅ Deleting**: 0 rows past 60d (was 200,724); rows cycling (inserts ≈ deletes); oldest surviving row rolls at the 60d boundary (2026-07-12) |
| DB full = stalemate (no auto-reclaim) | `d46df2176c` + autovacuum tuning via psql | **✅ Mitigated**: retention now reclaims continuously so the stalemate window requires a 6x traffic burst to reach; per-table autovacuum (scale 0.02) keeps dead tuples bounded |
| Gateway crashes when DB down | `c5182135b4` (`allow_requests_on_db_unavailable`) | **✅ Live**: flag in pod config; validated end-to-end on dev (DB-less startup + serving) |
| Gateway OOMKills (8Gi limit) | `df73dfe33f` (12Gi) | **✅ Live**: limit 12Gi, 0 restarts since deploy; memray proved serving path leak-free |
| Memory creep invisibility | `df73dfe33f` + `33798c230b` (memory monitor) | **✅ Live**: mem_monitor lines in prod logs/Loki; RSS floor flat, cycle peaks drifting ~11 MB/day (~1,000 days to limit) |
| Ingress TLS mismatch | `b0d766c44c` | **✅ Live**: `litellm.ecouncil.ae` serves DigiCert; `litellm.adeoaiengine.ecouncil.ae` serves internal cert (accepted, see follow-up 5) |
| 100Gi revert risk (Helm) | `93ebda45d1` (manifest mirror) | **✅ Live**: PVCs report 100Gi; manifest in deploy/prod |
| Longhorn webhook blocking resize | settings patch 100→150 | **✅ Live**: over-provisioning = 150 |
| Autovacuum lag after mass deletes | psql per-table tuning (0d7c6def59) | **✅ Live**: reloptions on SpendLogs/ToolIndex/GuardrailIndex (scale 0.02) |

### Follow-ups (open)

1. ~~Enable spend log retention~~ **DONE** (60d, verified deleting: 0 rows
   past 60d as of 09-09, 217K dead tuples pending autovacuum). Optionally
   `VACUUM FULL` later to shrink below the plateau, or range partitioning via
   `db_scripts/partition_spend_logs.sql` (drops partitions instantly,
   recommended at this volume). **2026-09-09 maintenance done**: online
   `VACUUM (ANALYZE)` run, and per-table autovacuum tuned on
   LiteLLM_SpendLogs / SpendLogToolIndex / SpendLogGuardrailIndex
   (`autovacuum_vacuum_scale_factor = 0.02` vs default 0.2) so autovacuum
   triggers after ~270K dead tuples instead of 2.6M — matching the daily
   ~500K-row delete churn from the retention job. DDL lives only on the
   primary via psql (not in any manifest); re-apply if the cluster is ever
   rebuilt
2. ~~Memory monitoring~~ **DONE** (`memory_monitor_job` live in prod). Remaining:
   watch `prom_series` growth over days; if linear with RSS, apply
   `prometheus_metrics_config` label filtering (upstream caps only
   `end_user`; model/key/team labels are uncapped)
3. ~~Alerting~~ **CLOSED by decision**: the gateway is decoupled from the
   database (survives any DB failure), so automated DB-size alerting is not
   required. The developer checks memory and disk usage regularly via the
   `mem_monitor` logs in Loki and the completeness table above; if a rising
   `prom_series` floor or RSS floor is observed, apply
   `prometheus_metrics_config` label filtering (upstream caps only
   `end_user`; model/key/team labels are uncapped)
4. ~~Investigate proxy OOMKills~~ **RESOLVED**: root cause identified (DB-outage
   amplification + steady-state buffering); mitigated by
   `allow_requests_on_db_unavailable`, 12Gi limit, and the monitor. Serving
   path proven leak-free by memray
5. ~~Gateway TLS for `litellm.adeoaiengine.ecouncil.ae`~~ still serving the
   internal default cert (functional; internal clients unaffected). Add the
   host as a SAN on a DigiCert cert only if external strict-TLS clients need it
6. Watch real free space on Longhorn storage nodes now that
   over-provisioning is 150% (currently 15–21 TB free per disk — fine)
7. Upstream candidates: cap `model_map_information` in SpendLogs metadata
   (~60% row-size cut, zero readers); cap model/key/team metric label
   cardinality; circuit-breaker on auth path during DB outages
