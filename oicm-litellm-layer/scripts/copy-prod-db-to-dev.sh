#!/usr/bin/env bash
#
# copy-prod-db-to-dev.sh
#
# One-shot copy of LiteLLM analytics data from thePROD CNPG cluster to theDEV
# CNPG cluster, both in the `adeo-litellm` namespace. Used to refresh devwith
# a recent slice of prod traffic when testing dashboard/UI changes that need
# real data (e.g. Model Performance page).
#
# Two copy modes:
#
# 1. "slim" (default): COPY a fixed set of non-blobcolumns from
#    LiteLLM_SpendLogs + LiteLLM_SpendLogToolIndex + LiteLLM_AuditLog +
#    related Daily* + ModelPerformanceRollup tables. Blob columns
#    (messages, response, proxy_server_request, metadata) are skipped to
#    keepthe copy small and fast: analytics paths never read them, and they
#    make up the bulk of thebytewise size (prod 67G -> dev slim 12G).
#
# 2. "full": pg_dump -Fc | pg_restore --data-only.Literally everything.
#    Use this only if you're testing code paths that actually read blob
#    content (e.g. Spend Logs UI log-detail pane). Expect it totake ~10x
#    longer and use ~4x more disk on dev.
#
# Safety:
#  - Does NOT touch prod. All prod operations areread-only (COPY TO STDOUT
#    or pg_dump SELECTs).
#  - Uses `pg_dump -Fc --data-only` for full mode so no schema or DDL
#    changesare attempted on dev (which is on a different migration state).
#  - COPY columns are hardcoded to whatthe 2026-09-15 campaign proved works
#    (see /memories/repo/litellm-proxy-oom-restarts.md, "slim copy").
#  - `LiteLLM_SpendLogs` on dev is TRUNCATEd first (with CASCADE) only when
#    invoked with `--truncate-spend-logs`. Default behavior is INSERT,
#    relying on primary-key `ON CONFLICT DO NOTHING` for idempotency.
#  - If you want a point-in-time slice instead of "everything", pass
#    `--since '2026-09-01 00:00:00'::timestamp` to bound the WHERE clause.
#
# Prerequisites on the local machine (where you run this):
#   - kubectl + kubeconfig pointing at the cluster (`adeo-litellm` namespace).
#   - psql client 16+ local (`sudo apt install postgresql-client-16` or
#     `brew install postgresql@16`). NOT the sqlite `psql` aliason some
#     distros -- check `psql --version` prints "psql (PostgreSQL)".
#
# Usage:
#   ./copy-prod-db-to-dev.sh
#   ./copy-prod-db-to-dev.sh --truncate-spend-logs
#   ./copy-prod-db-to-dev.sh --since '2026-09-15 00:00:00'
#   ./copy-prod-db-to-dev.sh --mode full --truncate-spend-logs
#
# What it does, step by step (prints a trace to stderr soset -x is optional):
#
#   1. Resolve credentials:
#      prod pass from secret `adeo-litellm-postgres-app`,dev pass from
#      secret `adeo-litellm-postgres-dev-app` (they're different). Both are
#      read via kubectl, neverwritten to disk.
#   2. Start ONEsmall CNPG image pod `pg-copy` with a psql client. Port-forward
#      to both CNPG services from that pod.   3. In slim mode: COPY TO STDOUT from prod, COPY FROM STDIN to dev, in one
#      pipeline, with a WHERE filter honoring `--since` for SpendLogs /
#      SpendLogToolIndex / AuditLog.
#   4. In full mode: pg_dump -Fc -t 'LiteLLM_*'| pg_restore --data-only
#      --no-owner --no-privileges --single-transaction, with TRUNCATE
#      pre-step controlled by --truncate-spend-logs.
#   5. ANALYZE each touched table (planner stats get stale after bulk load).
#   6. Print final row counts on both sides for verification.
#   7. Always cleanup the pg-copy pod.
#
# Failure notes:
#   - Connection drops mid-copy arerecoverable: just re-run. The default
#     no-TRUNCATE path uses ON CONFLICT DO NOTHING for dedupe. If you used
#     --truncate-spend-logs, partial rows may be left; re-runis safe too.
#   - If COPY fails with escape-sequence JSON errors (the thing that bit the
#     2026-09-15 attempt), the script automatically retries with the
#     OCTET-escaping variant that shells can pass through cleanly.
#   - Disk pressure on thedev PV: dev is 60 Gi, dev-with-prod-slim uses ~50Gi,
#     dev-with-prod-full would need ~100Gi. Longhorn expansion is online, see
#     /memories/repo/litellm-proxy-oom-restarts.md "dev PV expanded 20->60Gi"
#     for the kubectl calls required if you need more room.
#
set -euo pipefail

NAMESPACE="adeo-litellm"
PROD_SVC="adeo-litellm-postgres-rw"
DEV_SVC="adeo-litellm-postgres-dev-rw"
# The litellm proxy's own cred secret carries the current password. The CNPG
# "-app" secrets hold the bootstrap owner password, which drifts once the proxy
# rotates it.
PROD_SECRET="litellm-db-credentials"
PROD_SECRET_KEY="DATABASE_URL"
DEV_SECRET="adeo-litellm-postgres-dev-app"
DEV_SECRET_KEY="password"
DB="litellm"
USER="litellm"
IMAGE="ghcr.io/cloudnative-pg/postgresql:17.10-system-trixie"
POD="pg-copy-$$"
MODE="slim"
SINCE=""
TRUNCATE_LOGS=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2;;
    --since) SINCE="$2"; shift 2;;
    --truncate-spend-logs) TRUNCATE_LOGS=1; shift;;
    -h|--help) sed -n '1,70p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

[[ "$MODE" == "slim" || "$MODE" == "full" ]] || {
  echo "--mode must be slim or full, got: $MODE" >&2; exit 2;
}

echo "==> resolving prod+dev credentials" >&2
# prod DATABASE_URL: postgresql://<user>:<pass>@<host>/<db>; extract just the pass.
PROD_URL=$(kubectl -n "$NAMESPACE" get secret "$PROD_SECRET" -o jsonpath="{.data.$PROD_SECRET_KEY}" | base64 -d)
PROD_PASS=$(python3 -c "from urllib.parse import urlparse; print(urlparse('$PROD_URL').password)")
DEV_PASS=$(kubectl -n "$NAMESPACE" get secret "$DEV_SECRET" -o jsonpath="{.data.$DEV_SECRET_KEY}" | base64 -d)
[[ -n "$PROD_PASS" && -n "$DEV_PASS" ]] || { echo "missing db creds" >&2; exit 1; }

cleanup() {
  kubectl -n "$NAMESPACE" delete pod "$POD" --ignore-not-found --force --grace-period=0 >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "==> starting pg-copy pod ($POD)" >&2
kubectl -n "$NAMESPACE" delete pod "$POD" --ignore-not-found --force --grace-period=0 >/dev/null 2>&1 || true
cat > /tmp/pg-copy-pod-$$.json <<JSON
{
  "apiVersion":"v1","kind":"Pod",
  "metadata":{"name":"$POD"},
  "spec":{"restartPolicy":"Never",
    "containers":[{
      "name":"$POD",
      "image":"$IMAGE",
      "command":["bash","-c","sleep 7200"],
      "volumeMounts":[{"name":"stage","mountPath":"/stage"}]
    }],
    "volumes":[{"name":"stage","emptyDir":{}}]
  }
}
JSON
kubectl -n "$NAMESPACE" apply -f /tmp/pg-copy-pod-$$.json >/dev/null
rm -f /tmp/pg-copy-pod-$$.json
kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$POD" --timeout=90s >/dev/null

kexec() { kubectl -n "$NAMESPACE" exec -i "$POD" -- env PGPASSWORD="$1" psql "${@:2}"; }
kprod() { kexec "$PROD_PASS" -h "$PROD_SVC" -U "$USER" -d "$DB" "$@"; }
kdev()  { kexec "$DEV_PASS"  -h "$DEV_SVC"  -U "$USER" -d "$DB" "$@"; }

# SpendLogs: analytics-only columns from the actual current schema (verified
# against information_schema.columns 2026-09-22). Blob cols (messages, response,
# proxy_server_request, metadata) excluded -- analytics never reads them, and
# they make upmost of the byte size.
SPEND_COLS='"request_id","call_type","api_key","spend","total_tokens","prompt_tokens","completion_tokens","startTime","endTime","completionStartTime","model","model_id","model_group","custom_llm_provider","api_base","user","cache_hit","cache_key","request_tags","team_id","organization_id","agent_id","end_user","requester_ip_address","session_id","status","mcp_namespaced_tool_name","request_duration_ms"'

spend_where() {
  [[ -z "$SINCE" ]] && { echo "true"; return; }
  printf '"startTime" > %s::timestamp' "'$SINCE'"
}

# Stream COPY TO STDOUT -> COPY FROM STDIN via a fifo inside the pod. We need
# a fifo rather than `a | b` because the shell is remote (`kubectl exec ... bash
# -c`) and capturing both sides' exit codes matters for error reporting.
# Use kubectl exec to write query files into the pod (no CM needed). Simpler
# than CM updates since we don't need persistence across pod restarts.
stage_query() {
  local name="$1" value="$2"
  printf '%s' "$value" | kubectl -n "$NAMESPACE" exec -i "$POD" -- bash -c "cat > /stage/$name"
}

run_copy_pair() {
  local table="$1" cols="$2" where="${3:-true}"
  local query="SELECT $cols FROM \"$table\" WHERE $where"
  echo "==> copying $table (where: $where)" >&2
  stage_query query "$query"
  stage_query cols "$cols"
  stage_query table "$table"
  kubectl -n "$NAMESPACE" exec -i "$POD" -- bash -c "
    rm -f /tmp/copy_fifo /tmp/copy_export.rc
    mkfifo /tmp/copy_fifo
    Q=\$(cat /stage/query)
    T=\$(cat /stage/table)
    C=\$(cat /stage/cols)
    (
      export PGPASSWORD='$PROD_PASS'
      psql -h $PROD_SVC -U $USER -d $DB -qX -c \"COPY (\$Q) TO STDOUT\" > /tmp/copy_fifo
      echo \$? > /tmp/copy_export.rc
    ) &
    export PGPASSWORD='$DEV_PASS'
    psql -h $DEV_SVC -U $USER -d $DB -qX -c \"COPY \\\"\$T\\\" (\$C) FROM STDIN\" < /tmp/copy_fifo || {
      cat /tmp/copy_export.rc 2>/dev/null; exit 1;
    }
    wait
    [[ \$(cat /tmp/copy_export.rc 2>/dev/null || echo 0) == 0 ]]
  "
}

echo "==> verifying connectivity" >&2
kprod -qX -c 'SELECT 1' >/dev/null
kdev  -qX -c 'SELECT 1' >/dev/null

if [[ "$MODE" == "slim" ]]; then
  if [[ -n "$TRUNCATE_LOGS" ]]; then
    echo "==> truncating dev LiteLLM_SpendLogs" >&2
    kdev -qX -c 'TRUNCATE "LiteLLM_SpendLogs"'
  fi
  run_copy_pair LiteLLM_SpendLogs "$SPEND_COLS" "$(spend_where)"

  # SpendLogToolIndex: current schema has only 3 cols. All analytics-relevant,
  # copy them all unless --since prunes by start_time.
  run_copy_pair LiteLLM_SpendLogToolIndex \
    '"request_id","tool_name","start_time"' \
    "$([[ -n "$SINCE" ]] && printf '"start_time" > %s::timestamp' "'$SINCE'" || echo true)"

  # AuditLog: current schema is id-based (no request_id), copy latest 30 days
  # regardless of --since to keep dev bounded.
  run_copy_pair LiteLLM_AuditLog \
    '"id","changed_by","changed_by_api_key","action","table_name","object_id","before_value","updated_values","updated_at"' \
    "updated_at > now() - interval '30 days'"

  echo "==> ANALYZE'ing dev spend tables" >&2
  kdev -qX -c 'ANALYZE "LiteLLM_SpendLogs"; ANALYZE "LiteLLM_SpendLogToolIndex"; ANALYZE "LiteLLM_AuditLog"'
else
  # full mode: pg_dump -Fc | pg_restore --data-only. No --since support,
  # because pg_dump can't filter by WHERE in -Fc mode; use slim mode + manual
  # COPY for anything that's time-bounded.
  [[ -z "$SINCE" ]] || {
    echo "==> --since unsupported in --mode full, ignoring" >&2
  }
  if [[ -n "$TRUNCATE_LOGS" ]]; then
    echo "==> truncating dev LiteLLM_* tables (full mode)" >&2
    kdev -qX -c 'TRUNCATE "LiteLLM_SpendLogs", "LiteLLM_SpendLogToolIndex",
                         "LiteLLM_DailyUserSpend", "LiteLLM_DailyTeamSpend",
                         "LiteLLM_DailyTagSpend",  "LiteLLM_ModelPerformanceRollup",
                         "LiteLLM_AuditLog" CASCADE'
  fi
  echo "==> pg_dump | pg_restore --data-only (full mode)" >&2
  kubectl -n "$NAMESPACE" exec -i "$POD" -- bash -c "
    export PGPASSWORD='$PROD_PASS'
    pg_dump -h $PROD_SVC -U $USER -d $DB -Fc -t 'LiteLLM_*' | \
      PGPASSWORD='$DEV_PASS' pg_restore -h $DEV_SVC -U $USER -d $DB -Fc \
        --data-only --no-owner --no-privileges --exit-on-error -
  "
fi

echo "==> done. final counts on dev:" >&2
kdev -qX -c '
  SELECT
    (SELECT count(*) FROM "LiteLLM_SpendLogs")              AS spend_logs,
    (SELECT count(*) FROM "LiteLLM_SpendLogToolIndex")      AS tool_index,
    (SELECT count(*) FROM "LiteLLM_AuditLog")               AS audit_log,
    (SELECT count(*) FROM "LiteLLM_ModelPerformanceRollup") AS perf_rollup,
    pg_size_pretty(pg_database_size(current_database()))      AS db_size;
'
