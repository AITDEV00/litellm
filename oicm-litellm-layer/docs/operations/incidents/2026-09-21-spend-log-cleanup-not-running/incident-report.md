# 2026-09-21 Postgres SpendLogCleanup never firing despite retention config

Severity: **reopened / active (2026-10-08)**. Originally deferred: Postgres disk
at 73%, table held ~22.5k rows beyond the 60-day cutoff, and no upstream cleanup
jobs had run in 24h. As of 2026-10-08 the pressure-based `spend-logs-janitor-prod`
CronJob is failing every run on a partition-permission error, so retention is no
longer being enforced: 1.59M rows are past the cutoff and the DB is at 81.7GB,
over the janitor's 75GiB pressure threshold. See "UPDATE 2026-10-08" below.

Status: RECORDED, then reopened 2026-10-08. Track via this file.

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

## UPDATE 2026-10-08 — janitor is now failing on permissions, retention has regressed

The cleanup problem changed shape: upstream `SpendLogCleanup` is still not the
actor, but the pressure-based `spend-logs-janitor-prod` CronJob that took over
retention is now **failing every run**.

Live state (2026-10-08):

| Metric | 2026-09-21 | 2026-10-08 |
|---|---|---|
| `spend-logs-janitor-prod` jobs | running | **5 consecutive Failed**, last 3 runs Complete were ~19-23h earlier |
| `older_than_60d` rows | 22,539 | **1,589,805** |
| Oldest row | 2026-07-23 | 2026-07-26 |
| DB size | 71GB / 99GB (73%) | **81.7GB**, over the janitor's 75GiB pressure threshold |
| `live_rows` | 16.8M | 19.07M |

Failure (from a failed job's pod log):

```
[janitor] disk pressure detected (76G > 75G); archiving oldest partitions
[janitor] working on LiteLLM_SpendLogs_p20260725
[janitor] DUMP LiteLLM_SpendLogs_p20260725 -> /archive/LiteLLM_SpendLogs_p20260725.dump
ERROR:  permission denied for table LiteLLM_SpendLogs_p20260725
[janitor] ERROR: count failed
```

Root cause: the janitor CronJob connects as `DB_USER=oicm`
(`deploy/prod/spend-logs-janitor/cronjob.yaml`), but the spend-log partitions are
owned by `postgres`:

```
LiteLLM_SpendLogs_p20260725  owner=postgres
LiteLLM_SpendLogs_p20260726  owner=postgres
```

`sql/bootstrap.sql` was written for a DB where the master table and its
partitions were owned by `litellm` (its `ALTER TABLE ... OWNER TO litellm` loop
is a no-op when the owner is already `postgres`), so the grants it issues to
`oicm` (ledger R/W, `CREATEDB`, `litellm` membership) never include SELECT on
the partitions themselves. `pg_dump` and `DETACH` therefore fail. The
`LiteLLM_SpendLogsArchiveLedger` is also owned by `oicm`, so the proxy
(`litellm`) gets `permission denied` reading it.

Fix direction (not yet applied; needs the CNPG superuser role):

- Either run the janitor as a role that owns or can read the partitions
  (`postgres`), or grant it explicitly:
  `GRANT SELECT ON ALL TABLES IN SCHEMA public TO oicm;` for the existing
  partitions, plus `ALTER TABLE <partition> OWNER TO oicm` (or `TO litellm`) so
  `DETACH`/`DROP` work, plus `ALTER DEFAULT PRIVILEGES FOR ROLE postgres ...`
  so future partitions stay readable.
- Re-run `sql/bootstrap.sql` after fixing ownership so the ledger ownership is
  consistent with the connecting role.

Until then, retention is not being enforced and the DB keeps growing past the
janitor's pressure threshold.

## To resume

- Verify whether `cleanup_old_spend_logs` ever appears in async-task dumps
  on a *freshly booted* pod (not one surviving a config reload).
- If the job registers but never fires, investigate
  `BaseRoutingStrategy.periodic_sync_in_memory_spend_with_redis` — the 3005
  zombie tasks — as the starvation source.
- If the job never registers, audit the `initialize_scheduled_background_jobs`
  code path for config-driven skips (e.g., a dynamic general-settings
  override clearing `maximum_spend_logs_retention_period` after boot).
