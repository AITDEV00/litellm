# Spend-Log Retention Flags + Gateway OOM — Logic Map v2

Phase 1 trace correcting `spend-logs-ram-and-model-performance-LOGIC-MAP.md`
(one claim in §2 was wrong — see §3.2). Live-verified against prod
(mlops, 2026-09-08) as `LIVE:`. All four deployed general_settings flags
traced to their exact source semantics.

---

## 1. The four flags — exact semantics

### 1.1 `maximum_spend_logs_retention_interval: "6h"`

- **What it controls**: HOW OFTEN the cleanup job fires, NOT how long logs
  live (that's `maximum_spend_logs_retention_period`).
- Read at `proxy_server.py:6398` (`_reschedule_spend_log_cleanup_job`):
  `general_settings.get("maximum_spend_logs_retention_interval", "1d")` →
  `duration_in_seconds()` → APScheduler IntervalTrigger with stagger
  (`stagger_trigger`, misfire grace APSCHEDULER_MISFIRE_GRACE_TIME).
- Default `"1d"`. Our `"6h"` → job fires 4x/day per pod; only ONE pod runs
  it per cycle (PodLockManager Redis SET NX EX 60s TTL,
  `pod_lock_manager.py:66-80`).
- FIRST RUN TIMING: interval jobs fire one interval AFTER registration, so
  first cleanup is T+6h after restart, not immediate.
- Hot-reloadable: `_reschedule_spend_log_cleanup_job` is called by
  `_update_general_settings` (DB config reload) and via
  `update_spend_config` paths — UI/API changes reschedule without restart.
- Drain capacity math: `max_batches × batch_size` per run × runs/day:
  - before: 500 × 1000 × 1 = 500K rows/day
  - after: 2000 × 1000 × 4 = 8M rows/day (vs ~470K/day traffic)

### 1.2 `maximum_spend_logs_cleanup_max_batches: 2000`

- Read per-run (not per-startup!) in `spend_log_cleanup.py:124`
  `_refresh_bounds`: `self.max_batches = self._positive_int_setting(
  "maximum_spend_logs_cleanup_max_batches", SPEND_LOG_RUN_LOOPS=500)`.
- Enforcement point: `_delete_old_rows_batched` loop at
  `spend_log_cleanup.py:~700`: `if run_count >= self.max_batches: return
  TableCleanupResult(..., "batch_cap_reached")`.
- Each "batch" = one `DELETE ... WHERE (pk) IN (SELECT ... LIMIT 1000)`
  statement (`spend_log_cleanup.py:660-667`), bounded by
  `SPEND_LOG_CLEANUP_BATCH_SIZE=1000` (constants.py:1532), with 0.1s sleep
  between batches (line ~755).
- 2000 batches × 1000 rows = 2M rows MAX per table per run — if the loop
  finishes the backlog first, it stops early ("exhausted").
- Hot-reloadable: `_refresh_bounds()` is called in EVERY
  `cleanup_old_spend_logs()` run (spend_log_cleanup.py:597-599), so a UI
  change takes effect next run.

### 1.3 `maximum_spend_logs_cleanup_run_budget: "20m"`

- Read per-run in `_refresh_bounds` (spend_log_cleanup.py:127-128):
  `_duration_setting("maximum_spend_logs_cleanup_run_budget",
  SPEND_LOG_CLEANUP_RUN_BUDGET_SECONDS=300)`.
- Semantics: a SHARED wall-clock deadline across ALL tables cleaned in one
  run (spend logs, tool index, autorouter sessions, health checks) —
  `cleanup_old_spend_logs` computes `deadline = time.monotonic() +
  run_budget_seconds` and passes it down.
- Enforced at four levels, ALL with statement timeouts clamped to the
  REMAINING budget (`_timeout_ms`, spend_log_cleanup.py:225-232):
  1. loop check `time.monotonic() >= deadline` → "budget_exhausted" (next
     run resumes — deletes are idempotent, cutoff recomputed)
  2. per-batch `SET LOCAL statement_timeout = remaining_ms` — Postgres
     cancels a statement that runs past the deadline
  3. `SET LOCAL lock_timeout = remaining_ms` — a batch stuck behind a long
     reader is cancelled, not queued indefinitely
  4. partition DDL (DROP PARTITION) skipped entirely once budget spent
     (ACCESS EXCLUSIVE lock would queue behind readers)
- Why "20m" not "300s": default 300s gives ~4-5 min of actual deleting per
  run at backlog sizes we saw; 20m × 4 runs = 80 min/day of deleting
  capacity. The bound exists so cleanup can never monopolize the DB — it
  is a ceiling, not a target; runs usually end "exhausted" long before.
- Stop-reason precedence for the run metric (`_run_outcome`): aborted >
  budget_exhausted > batch_cap_reached > completed.

### 1.4 `allow_requests_on_db_unavailable: true`

- Read in THREE places, all config-driven, no code paths change:
  1. **Auth fallback** — `auth_exception_handler.py:80-108`:
     when `should_allow_request_on_db_unavailable()` is True AND the error
     is classified a DB connection error (`exception_handler.py:127-168`
     — includes prisma's mislabeled P1001), the handler returns a
     RESTRICTED synthetic identity instead of raising:
     `UserAPIKeyAuth(key_name="failed-to-connect-to-db", user_role=
     INTERNAL_USER, user_id="__db_unavailable_fallback__")`.
     Deliberately non-admin (no privilege escalation during an outage).
     Without the flag: 503 `no_db_connection` on auth cache miss.
  2. **Startup** — `proxy_server.py:9608-9620`: `prisma_client.connect()`
     failure calls `handle_db_exception`, which re-raises UNLESS this flag
     is set; with it, startup retains the client and the DB health
     watchdog reconnects when Postgres returns (no CrashLoopBackOff).
  3. **Post-connect health check** — same handler; a failed
     `health_check()` (3 backoff tries, ~10s) after successful connect
     also retains instead of raising.
- What it does NOT do: does not disable logging, does not skip budget
  checks silently (spend enforcement degrades to Redis-cached values,
  warns at proxy_server.py:8525-8544), does not make writes succeed.
- Cost during outage: every auth cache miss still ATTEMPTS the DB (with a
  bounded 2s reconnect budget, auth_checks.py:3132-3175) before falling
  back — the flag changes the OUTCOME (serve instead of 503), not the
  latency. That latency is part of why the event loop saturates (§3).

## 2. What these flags do NOT fix

- The unbounded in-pod lists (§3.2) — upstream bug, needs code or issue.
- Steady-state ~1.6 Gi/day RSS growth from per-request buffering
  (independent of DB state).
- Alerting (Alertmanager receivers undefined; kubelet_volume_stats gone on
  kubelet v1.34; working metric: cnpg_pg_database_size_bytes).

## 3. What caused the OOM — corrected logic map

### 3.1 Corrected drain-order claim (§3.2 of the old map was WRONG)

Old claim: "tool/autorouter drains are SKIPPED when the spend flush
raises". Verified false — the real structure of `update_spend_logs_job`
(utils.py:6301):

```
dequeue_spend_logs(10_000)                          utils.py:6319
await ProxyUpdateSpend.update_spend_logs(...)       utils.py:6324
    └─ on transport error: retry 3x, requeue at_head, RAISE   utils.py:6143
    └─ raise propagates OUT of the job — the tool/guardrail/autorouter
       drains below ARE skipped for THIS invocation...
BUT the raise is CAUGHT by the queue monitor loop    utils.py:6486
    (except Exception → log, backoff, continue)
and the NEXT monitor tick calls update_spend_logs_job again — which
    re-dequeues (empty of spend logs) and RUNS the drains.
```

So the drains are delayed by one monitor tick (2s base / 30s max backoff),
NOT skipped. Corrected mechanism for the lists' growth during the incident:

- The drain batches are capped at `MAX_LOGS_PER_INTERVAL = 10_000`
  (utils.py:6313) per job invocation; the monitor fires at most every 2s.
  At 470K req/day ≈ 5.4 req/s average, one 10K drain every 2s is plenty —
  the lists should NOT grow from this alone.
- BUT during the incident, each drain call itself makes DB calls
  (`flush_tool_usage_transactions` → tool index INSERTs +
  `LiteLLM_DailyToolSpend` upserts). Those calls pay the same
  reconnect/timeout dance (up to 2s+ each, engine subprocess churn), so
  each monitor cycle takes longer, the 30s cap binds, and the drain RATE
  (≤10K/30s = 20K/min) approaches the arrival rate. Combined with request
  pileup (§3.3), the lists can still grow — but bounded-ish and slow.
- Verdict correction: the lists are UNBOUNDED (true, db_spend_update_writer.py:393,435)
  and COULD grow without limit, but they were probably NOT the primary OOM
  driver on 09-08. The stronger drivers are below.

### 3.2 Primary OOM drivers (ranked by evidence)

1. **Event-loop saturation → liveness timeout → k8s restarts.** The
   /health/liveliness handler (health_endpoints/_health_endpoints.py)
   touches nothing — its timeout is definitive proof the loop was
   starved. Sources of starvation, per worker (4 granian workers):
   - every monitor tick + scheduler tick rebuilds ≤10K-row batches and
     serializes them (jsonify_object deep-copies; ≤2MB/100-row statement
     splitting, spend_log_batching.py:118) — against a dead DB, retried
     and requeued every cycle
   - auth cache misses: up to 2s synchronous-ish reconnect budget per
     request during the outage (auth_checks.py:3132-3175) — under load,
     hundreds of concurrently-awaiting auth coroutines
   - prisma engine churn: heavy reconnect spawns a NEW query-engine
     subprocess every ~15s of continued failure per worker
     (utils.py:3492, 5067-5150) — each is a child process in the same
     8Gi cgroup; spawn/drain cycles hold RSS spikes
   - Redis buffer pop/restore cycle on the leader: pops ≤100/queue,
     commits fail, re-RPUSHes everything — payload grows with outage
     duration (redis_update_buffer.py:383-424), and safe_dumps of it
     allocates transiently on every tick
2. **RSS ratchet from per-request transients under a saturated loop.**
   get_logging_payload deep-copies bodies per request; asyncio tasks
   awaiting the (slow) auth path each retain their request payload; GC
   falls behind allocation under CPU starvation → steady climb to 8Gi.
3. **The unbounded lists (§3.1)** — real but slower-growing; a
   contributing factor, not the primary.

### 3.3 Why OOM appeared ~2-3 days after pod start (Sep 5, Sep 7 kills)

Two superposed growths, both observed:
- DB-incident-driven (Sep 7): the disk-full outage starting ~07:56
  triggered the §3.2 machinery for ~40 minutes → liveness timeouts →
  restart. Consistent with the restart at 11:50 TODAY clearing memory
  (6.7→3.0 Gi).
- Steady-state accumulation (Sep 5 kill, before any DB trouble): ~1.6
  Gi/day RSS growth from per-request buffering, fragmentation, metric
  cardinality — independent of DB. This is the remaining risk the flags
  do NOT fix.

## 4. LIVE verification

- Flags in pod config verified: `grep allow_requests /app/config.yaml` →
  true; retention_interval/max_batches/run_budget present (11:53 UTC).
- Post-deploy: both pods ~3.0-3.1 Gi (restart reset), CPU <350m.
- First 6h cleanup run expected ~17:50 UTC; verify via
  `SpendLogCleanupMetrics` gauge `litellm_spend_log_rows_remaining` or
  `SELECT COUNT(*) ... startTime < NOW()-'60d'` dropping.
