# Spend Logs Archival (Dev)

Keep spend logs around without the heap of a one-table DB. The pipeline is:
LiteLLM serves requests, writes raw rows into partitioned `LiteLLM_SpendLogs`,
and this CronJob selectively archives past-day partitions onto a dedicated
PVC so any day's raw rows can be recalled on demand.

## Architecture

- **Upstream LiteLLM** owns partition creation and the "when is this day
  over" routing via `use_spend_logs_partitioning: true`. We set retention_period
  to `999d` so LiteLLM never drops anything itself — that's our job.
- **This CronJob** is the only custom component. It maintains the ledger and
  walks each partition through `eligible → detached → dumped → verified →
  dropped` one step per minute.
- **PVC** `spend-logs-archive` stores compressed pg_dump custom-format files.
  `pg_restore` + `ATTACH PARTITION` restores any archived partition on demand.

## Files

- `cronjob.yaml` — embedded `janitor.sh` + CronJob definition.
- `pvc.yaml` — the archive volume.
- `sql/bootstrap.sql` — ledger + meta tables, ownership, grants, master-table guard.
- `scripts/restore.sh` — manual restore helper for a named partition.

## Operation

Inspect:
```bash
kubectl -n adeo-litellm exec -it adeo-litellm-postgres-dev-1 --container postgres -- \
  psql -U postgres -d litellm -c \
  "SELECT partition_name, state, rows_at_dump FROM \"LiteLLM_SpendLogsArchiveLedger\" ORDER BY bound_upper"
```

Restore:
```bash
kubectl -n adeo-litellm run myrestore \
    --rm -it \
    --image=ghcr.io/cloudnative-pg/postgresql:17.5 \
    --overrides='{"spec":{
      "restartPolicy":"Never",
      "volumes":[{"name":"archive","persistentVolumeClaim":{"claimName":"spend-logs-archive"}}],
      "containers":[{"name":"myrestore","image":"ghcr.io/cloudnative-pg/postgresql:17.5","command":["bash","-c","sleep 600"],"volumeMounts":[{"name":"archive","mountPath":"/archive"}]}]}
    }' -- bash -c "
    export DB_HOST=adeo-litellm-postgres-dev-rw DB_USER=litellm DB_NAME=litellm DB_PORT=5432
    export DB_PASSWORD=\$(kubectl -n adeo-litellm get secret adeo-litellm-postgres-dev-app -o jsonpath='{.data.password}' | base64 -d)
    bash /deploy/dev/spend-logs-janitor/scripts/restore.sh LiteLLM_SpendLogs_p20261020 /archive/LiteLLM_SpendLogs_p20261020.dump
  "
```

The restore script reads ledger bounds, verifies artifact checksum,
`pg_restore`s into a scratch DB, checks row counts, COPYs into an empty
shell table, ATTACHes the shell to the parent, marks ledger `restored`.

## Switching modes

Edit env on the CronJob or recreate from `cronjob.yaml`:
- `RETENTION_MODE=days` — only `RETENTION_DAYS`-old partitions are eligible.
- `RETENTION_MODE=pressure` — only trigger when
  `pg_database_size(current_database()) > PRESSURE_THRESHOLD_BYTES`. The
  `RETENTION_FLOOR_HOURS=24` floor still applies so today and yesterday
  aren't touched.

## What deviates from upstream LiteLLM

- One env-var addition on the dev config:
  `use_spend_logs_partitioning: true`, `maximum_spend_logs_retention_period: 999d`.
- Everything else (CronJob, PVC, ledger, restore script) is downstream;
  nothing in LiteLLM source code is changed or forked.
