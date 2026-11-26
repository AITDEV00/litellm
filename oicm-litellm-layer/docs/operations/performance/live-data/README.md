# Live-data evidence (analytics-reads prisma-engine OOM investigation)

Raw artifacts captured from prod (`adeo-litellm`, 2026-09-15) backing
`../analytics-reads-prisma-engine-oom-LOGIC-MAP.md`. Referenced from the
logic map's evidence tables as `live-data/<n>`. Regenerate from the cluster
rather than hand-edit.

| # | Artifact | What it proves |
|---|----------|----------------|
| 01 | `01-pod-memory-range-3d.json` | Prometheus 3-day `container_memory_working_set_bytes`: pod memory ratcheting in discrete +1-2GiB steps |
| 02 | `02-restart-counters.json` | Pod restart counters rising (both pods) |
| 03 | `03-last-terminated-oomkilled.json` | `last_terminated_reason = OOMKilled` on both pods — kills, not probes/rollouts |
| 04 | `04-pod-memory-0914-kill-window.json` | Memory curve inside the 09-14 kill window |
| 05 | `05-prisma-engines-process-memory.txt` | `ps` + `/proc/<pid>/smaps_rollup`: one engine at 5.06GB `Private_Dirty` anon; 13x128MiB + 17x64MiB glibc arenas ~100% resident |
| 06 | `06-pg-stat-statements-heavy-queries.txt` | `pg_stat_statements`: GROUPING SETS aggregate 310 calls / 1.49M rows (4,804/call); DISTINCT end_user calls averaging 7.2s |
| 07 | `07-db-table-sizes.txt` | `LiteLLM_SpendLogs` 57GB / 14.7M rows; `LiteLLM_DailyUserSpend` 34,442 rows |
| 08 | `08-grouping-sets-replay-rowcount.txt` | Replay of the full-range GROUPING SETS query: 26,816 rows in ONE statement |
| 09 | `09-ui-endpoint-traffic-24h.txt` | nginx 24h: `/model/performance` 28, `/user/daily/activity/aggregated` 16, `/gateway/daily/activity` 16 reads + UI spend-logs views |
| 10 | `10-deployment-config.txt` | Deployment shape: `-num_workers 4`, 12Gi limits |
