#!/bin/bash
set -euo pipefail

PGDATA="${PGDATA:-/var/lib/postgresql/data}"
POSTGRES_USER="${POSTGRES_USER:-litellm}"
POSTGRES_DB="${POSTGRES_DB:-litellm}"
PGPORT="${PGPORT:-5432}"

if [ -z "${POSTGRES_PASSWORD:-}" ]; then
    echo "[pg] POSTGRES_PASSWORD is required and must not be baked into the image" >&2
    exit 1
fi

mkdir -p "$PGDATA" /var/run/postgresql
chown -R postgres:postgres "$PGDATA" /var/run/postgresql
chmod 700 "$PGDATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    echo "[pg] empty data directory, initialising cluster"
    pwfile="$(mktemp)"
    printf '%s' "$POSTGRES_PASSWORD" >"$pwfile"
    chown postgres:postgres "$pwfile"
    # --encoding/--locale are required. This image has no locale archive, so a
    # bare initdb silently picks SQL_ASCII, and psycopg then returns bytes for
    # every text column, which crashes LiteLLM's migration bookkeeping with
    # "cannot use a string pattern on a bytes-like object". C locale needs no
    # locale files, and UTF8 keeps the server usable for non-ASCII data.
    gosu postgres initdb \
        --pgdata="$PGDATA" \
        --username="$POSTGRES_USER" \
        --pwfile="$pwfile" \
        --auth-local=trust \
        --auth-host=scram-sha-256 \
        --encoding=UTF8 \
        --locale=C
    rm -f "$pwfile"
fi

# Foreground, signalled directly. `pg_ctl start` would detach it, so a stop
# would never reach postgres and postmaster.pid would survive into the volume,
# breaking the next boot.
echo "[pg] starting postgres on port $PGPORT"
gosu postgres postgres -D "$PGDATA" \
    -c listen_addresses=127.0.0.1 \
    -c unix_socket_directories=/var/run/postgresql \
    -c port="$PGPORT" &
pg_pid=$!

app_pid=""

shutdown() {
    trap - TERM INT
    echo "[supervisor] shutting down"
    if [ -n "$app_pid" ]; then kill -TERM "$app_pid" 2>/dev/null || true; fi
    kill -TERM "$pg_pid" 2>/dev/null || true
    if [ -n "$app_pid" ]; then wait "$app_pid" 2>/dev/null || true; fi
    wait "$pg_pid" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

echo "[pg] waiting for readiness"
for _ in $(seq 1 120); do
    if gosu postgres pg_isready -h /var/run/postgresql -p "$PGPORT" \
        -U "$POSTGRES_USER" -d postgres -q; then
        break
    fi
    if ! kill -0 "$pg_pid" 2>/dev/null; then
        echo "[pg] postgres exited during startup" >&2
        wait "$pg_pid" || true
        exit 1
    fi
    sleep 0.5
done
gosu postgres pg_isready -h /var/run/postgresql -p "$PGPORT" -U "$POSTGRES_USER" -d postgres

if ! gosu postgres psql -h /var/run/postgresql -p "$PGPORT" -U "$POSTGRES_USER" \
    -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$POSTGRES_DB'" | grep -q 1; then
    echo "[pg] creating database $POSTGRES_DB"
    gosu postgres createdb -h /var/run/postgresql -p "$PGPORT" \
        -U "$POSTGRES_USER" "$POSTGRES_DB"
fi

# Exported, never echoed: the URL embeds the password and container logs are
# routinely collected.
if [ -z "${DATABASE_URL:-}" ]; then
    export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${PGPORT}/${POSTGRES_DB}"
fi

echo "[pg] ready"
echo "[app] starting: docker/prod_entrypoint.sh $*"
docker/prod_entrypoint.sh "$@" &
app_pid=$!

# `wait -n` returns the dead child's non-zero status, which `set -e` treats as
# fatal and exits on, skipping teardown and orphaning postgres.
wait -n "$pg_pid" "$app_pid" || true
echo "[supervisor] a process exited, stopping the other"
shutdown
