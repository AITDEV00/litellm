# 2026-09-21 Postgres SpendLogCleanup never firing despite retention config

Severity: deferred. Postgres disk at 73% (was 98%+ during the 2026-09-08
incident), table holds ~22.5k rows beyond the 60-day cutoff, and no cleanup
jobs have run in the last 24h. RAM is stable; the gateway itself is healthy.

Status: RECORDED, deferred. Will come back to this. Track via this file.

---

## Symptom

`LiteLLM_SpendLogs` table growth is continuing unbounded past the
configured `maximum_spend_logs_retention_period: "60d"`:

| Metric | Value | Expected |
|---|---|---|
| `live_rows` in `LiteLLM_SpendLogs` | 16.8M | Stable / slightly shrinking |
| `older_than_60d` rows | 22,539 | 0 |
| Oldest row | 2026-07-23 | Nothing older than 60d |
| Disk usage | 71GB / 99GB (73%) | Trending flat or down |
| `SpendLogCleanup` job log lines (24h) | 0 | "Lock acquisition attempt", "Released cleanup lock", or "Another pod is already running" |
| `/debug/asyncio-tasks` SpendLogCleanup entry | absent | present as scheduled job |

## Why it matters

This is the same shape of failure that caused the 2026-09-08 postgres
disk-full incident. At ~500k rows/day growth (current traffic level), the
50Gi → 99Gi PVC would refill in roughly 6-8 weeks. Not an emergency, but
silently brewing.

## Suspected contributing factors

1. The proxy's APScheduler-based background-job scheduler is not visibly
   running `SpendLogCleanup`. Other scheduled jobs (`SlackAlerting` daily
   report, config sync subscribers) *are* present in async-task dumps, so
   a scheduler exists; the cleanup job specifically is not there.
2. 3,005 orphaned `BaseRoutingStrategy.periodic_sync_in_memory_spend_with_redis`
   tasks indicate a separate leak in the Redis spend-sync loop; whether this
   is crowding out the scheduler or independent of it remains to be proven.
3. Pod restarts within the last 24h (the OOM rollout) may have reset the
   scheduler state without re-registering the cleanup job — needs to be
   verified against a fresh pod before assuming it's a chronic defect.

## Decision

Defer. Disk is at 73%, has months of headroom, and the gateway memory fix
is verified holding. The cost of fixing this wrong (false positive claiming
healthy cleanup) outweighs the cost of leaving it deferred.

## To resume

- Verify whether `cleanup_old_spend_logs` ever appears in async-task dumps
  on a *freshly booted* pod (not one surviving a config reload).
- If the job registers but never fires, investigate
  `BaseRoutingStrategy.periodic_sync_in_memory_spend_with_redis` — the 3005
  zombie tasks — as the starvation source.
- If the job never registers, audit the `initialize_scheduled_background_jobs`
  code path for config-driven skips (e.g., a dynamic general-settings
  override clearing `maximum_spend_logs_retention_period` after boot).
