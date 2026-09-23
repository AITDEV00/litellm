#!/usr/bin/env bash
# restore-spend-logs-partition.sh
# Restore a previously archived SpendLogs partition dump back into the dev DB
# and ATTACH it back as a partition so LiteLLM queries can see it again.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <partition_name> <dumpfile>"
    echo "Example: $0 LiteLLM_SpendLogs_p20260901 /archive/LiteLLM_SpendLogs_p20260901.dump"
    exit 1
fi

PARTITION="$1"
DUMPFILE="$2"
PSQL="${PSQL:-psql}"
LOCK_TIMEOUT="${LOCK_TIMEOUT:-15s}"
STATEMENT_TIMEOUT="${STATEMENT_TIMEOUT:-30min}"

DB_HOST="${DB_HOST:?required}"
DB_PORT="${DB_PORT:-5432}"
DB_NAME="${DB_NAME:-litellm}"
DB_USER="${DB_USER:-litellm}"
export PGPASSWORD="${DB_PASSWORD:?required}"
export PGCONNECT_TIMEOUT=10

log() { printf '[restore] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

run_psql() {
    PGOPTIONS="-c lock_timeout=${LOCK_TIMEOUT} -c statement_timeout=${STATEMENT_TIMEOUT}" \
        "${PSQL}" -q -v ON_ERROR_STOP=1 \
              -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" \
              "$@"
}

run_pgrestore() {
    PGOPTIONS="-c lock_timeout=${LOCK_TIMEOUT} -c statement_timeout=${STATEMENT_TIMEOUT}" \
        pg_restore -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$@"
}

# --- Resolve partition bounds from ledger ----------------------------------
LEDGER_ROW=$(run_psql -d "$DB_NAME" -Atc "
    SELECT bound_lower || '|' || bound_upper || '|' || state || '|' || COALESCE(artifact_sha256,'')
    FROM \"LiteLLM_SpendLogsArchiveLedger\"
    WHERE partition_name = '${PARTITION}'" \
    || { log "Ledger row missing; cannot determine bounds. Not attaching."; exit 1; })

if [[ -z "$LEDGER_ROW" ]]; then
    log "No ledger row for ${PARTITION}; this script needs the ledger to continue"
    exit 1
fi

BOUND_LOWER=$(cut -d'|' -f1 <<<"$LEDGER_ROW")
BOUND_UPPER=$(cut -d'|' -f2 <<<"$LEDGER_ROW")
EXPECTED_SHA=$(cut -d'|' -f4 <<<"$LEDGER_ROW")

if [[ -z "${EXPECTED_SHA}" ]]; then
    log "No artifact checksum in ledger for ${PARTITION}; was it ever dumped?"
    exit 1
fi

if [[ ! -f "${DUMPFILE}" ]]; then
    log "Dump file not found: ${DUMPFILE}"
    exit 1
fi

actual_sha=$(sha256sum "${DUMPFILE}" | awk '{print $1}')
if [[ "${actual_sha}" != "${EXPECTED_SHA}" ]]; then
    log "Checksum mismatch: ${actual_sha} != ${EXPECTED_SHA}; refusing to restore"
    exit 1
fi
RST_DB="restore_${PARTITION}"
run_psql -d "$DB_NAME" -c "DROP DATABASE IF EXISTS \"${RST_DB}\"" || true
run_psql -d "$DB_NAME" -c "CREATE DATABASE \"${RST_DB}\" WITH TEMPLATE template0"
run_pgrestore --exit-on-error --dbname="$RST_DB" "$DUMPFILE"

restored_rows=$(run_psql -d "$RST_DB" -Atc "SELECT count(*) FROM \"${PARTITION}\"")
ledger_rows=$(run_psql -d "$DB_NAME" -Atc "SELECT rows_at_dump FROM \"LiteLLM_SpendLogsArchiveLedger\" WHERE partition_name = '${PARTITION}'")
if [[ "${restored_rows}" != "${ledger_rows}" ]]; then
    log "Restored row-count (${restored_rows}) != ledger (${ledger_rows}); refusing to attach"
    run_psql -d "$DB_NAME" -c "DROP DATABASE \"${RST_DB}\""
    exit 1
fi

run_psql -d "$DB_NAME" -c "CREATE TABLE IF NOT EXISTS \"${PARTITION}\" (LIKE \"LiteLLM_SpendLogs\" INCLUDING DEFAULTS INCLUDING GENERATED);"

# COPY rows across via a client roundtrip: dump CSV from scratch DB to stdout
# and COPY into the real table. Streams, no server-side temp files needed.
"${PSQL}" -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$RST_DB" -c "\\COPY \"${PARTITION}\" TO STDOUT WITH CSV" | \
    "${PSQL}" -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "\\COPY \"${PARTITION}\" FROM STDIN WITH CSV"

run_psql -d "$DB_NAME" -c "DROP DATABASE \"${RST_DB}\""

run_psql -d "$DB_NAME" -c "
    ALTER TABLE \"${PARTITION}\" ADD CONSTRAINT ${PARTITION}_bounds_check CHECK (\"startTime\" >= '${BOUND_LOWER}' AND \"startTime\" < '${BOUND_UPPER}');
    ALTER TABLE \"LiteLLM_SpendLogs\" ATTACH PARTITION \"${PARTITION}\" FOR VALUES FROM ('${BOUND_LOWER}') TO ('${BOUND_UPPER}');
    ALTER TABLE \"${PARTITION}\" DROP CONSTRAINT ${PARTITION}_bounds_check;
"

run_psql -d "$DB_NAME" -c "UPDATE \"LiteLLM_SpendLogsArchiveLedger\" SET state = 'restored', transitioned_at = now() WHERE partition_name = '${PARTITION}'"
log "Attached. ${restored_rows} rows are now visible to LiteLLM again."
log "Return the partition to the archive by resetting its ledger state to 'eligible' and rerunning the janitor."
