#!/usr/bin/env bash
#
# port-forward-datasources.sh
#
# Port-forward the datasources the deployed `litellm-proxy` in the `mlops`
# namespace uses, so a LOCAL litellm instance can validate changes against the
# same real metrics backend. It intentionally does NOT forward any LLM model
# service -- only the stateful/observability backends:
#
#   datasource          namespace                 local port  ->  remote
#   ----------------    ------------------------  ----------      -----
#   Postgres (Prisma)   mlops/mlops-postgres-rw   5432       ->    5432
#   Redis  (cache)      redis/litellm-redis       16379      ->    6379
#   Prometheus (metrics)kube-prometheus-stack     9090       ->    9090
#
# Prerequisites:
#   - An SSH tunnel to the K8s API server is already up. The one in this repo:
#       sshpass -p 'Password123' ssh -fN -o StrictHostKeyChecking=no \
#         -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
#         -o ExitOnForwardFailure=yes \
#         -L 6443:10.34.104.10:6443 adeo@10.34.104.99
#   - kubectl on PATH, kubeconfig pointing at the Al Ain cluster.
#   - `jq` if you use the optional --validate (kubectl jsonpath works too).
#
# Usage:
#   ./port-forward-datasources.sh [--validate] [--verbose]

set -euo pipefail

NAMESPACE_MLOPS="mlops"
NAMESPACE_REDIS="redis"
NAMESPACE_PROM="kube-prometheus-stack"

# local:remote
POSTGRES_PF="5432:5432"
REDIS_PF="16379:6379"
PROM_PF="9090:9090"

VALIDATE=0
VERBOSE=0

for arg in "$@"; do
    case "$arg" in
        --validate) VALIDATE=1 ;;
        --verbose) VERBOSE=1 ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

log() { if [ "$VERBOSE" = "1" ]; then printf '[pf] %s\n' "$*" >&2; fi; }

# Verify the API server is reachable before opening forwards.
if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "ERROR: kubectl cannot reach the cluster. Is the SSH tunnel up?" >&2
    echo "  sshpass -p 'Password123' ssh -fN -o StrictHostKeyChecking=no \\" >&2
    echo "    -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \\" >&2
    echo "    -o ExitOnForwardFailure=yes -L 6443:10.34.104.10:6443 adeo@10.34.104.99" >&2
    exit 1
fi
echo "cluster-info: API reachable via tunnel"

PIDS=()
cleanup() {
    log "stopping port-forwards"
    for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

start_pf() {
    local ns="$1" svc="$2" mapping="$3"
    if [ "$VALIDATE" = "1" ]; then
        if ! kubectl -n "$ns" get svc "$svc" >/dev/null 2>&1; then
            echo "WARN: service $ns/$svc not found; skipping" >&2
            return
        fi
    fi
    log "forwarding $ns/$svc $mapping"
    kubectl -n "$ns" port-forward "svc/$svc" "$mapping" >/tmp/pf_${svc}.log 2>&1 &
    PIDS+=("$!")
}

# --- Postgres (LiteLLM Prisma DB) ---
start_pf "$NAMESPACE_MLOPS" "mlops-postgres-rw" "$POSTGRES_PF"

# --- Redis (cache / auth cache / transaction buffer) ---
start_pf "$NAMESPACE_REDIS" "litellm-redis" "$REDIS_PF"

# --- Prometheus (metrics queries for /model/performance, /global spend) ---
start_pf "$NAMESPACE_PROM" "kube-prometheus-stack-prometheus" "$PROM_PF"

sleep 1
echo "Port-forwards active (Ctrl+C to stop):"
echo "  Postgres:   127.0.0.1:5432   (mlops/mlops-postgres-rw)"
echo "  Redis:      127.0.0.1:16379  (redis/litellm-redis)"
echo "  Prometheus: 127.0.0.1:9090   (kube-prometheus-stack/prometheus)"

# Stay alive; on Ctrl+C the trap cleans up.
while true; do sleep 3600; done