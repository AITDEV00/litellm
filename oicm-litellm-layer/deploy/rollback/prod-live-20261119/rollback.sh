#!/usr/bin/env bash
#
# Restore the `adeo-litellm` namespace to its exact running state as of
# 2026-11-19T11:24Z, before the MCP source broker and digest-pinned prod
# manifests were deployed.
#
# This is a disaster path. Run it only if the new prod rollout is broken.
# It is idempotent: applying an unchanged object is a no-op.
#
# Usage, from the repo root:
#   bash oicm-litellm-layer/deploy/rollback/prod-live-20261119/rollback.sh
#
# Override the namespace (default adeo-litellm) with ROLLBACK_NAMESPACE.
# Override the kubeconfig with KUBECONFIG as usual.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
namespace=${ROLLBACK_NAMESPACE:-adeo-litellm}

echo "rolling $namespace back to the 2026-11-19 running state"
echo

# 1. Delete the objects the new rollout created. They do not exist in the
#    snapshot, so they must go or the namespace is left with a broker whose
#    Service and NetworkPolicies point at nothing. --ignore-not-found keeps a
#    second run harmless.
echo "removing objects introduced by the new rollout"
for ref in \
  "deployment/litellm-mcp-broker" \
  "service/litellm-mcp-broker" \
  "networkpolicy/litellm-mcp-broker" \
  "networkpolicy/litellm-rustfs-source-broker"
do
  kubectl -n "$namespace" delete "$ref" --ignore-not-found --wait=true >/dev/null
  echo "  removed $ref"
done
echo

# 2. Restore every object captured in the snapshot. The images inside are
#    pinned to digests, so this restores the bytes that were actually running,
#    not whatever a mutable tag points at today. Pass the directory, not a glob:
#    `kubectl apply -f` rejects more than one file argument.
echo "applying the captured running state"
kubectl apply -f "$here"
echo

# 3. Wait for the two workloads to come back healthy, so the command only
#    returns once the rollback has actually taken effect.
echo "waiting for rollouts"
kubectl -n "$namespace" rollout status deployment/litellm-proxy --timeout=600s
kubectl -n "$namespace" rollout status deployment/oicm-discovery-controller --timeout=600s
kubectl -n "$namespace" rollout status statefulset/rustfs --timeout=600s
echo

# 4. Prove the running bytes are the pinned ones, not the new ones.
echo "verifying running images"
kubectl -n "$namespace" get pods -l app=litellm-proxy \
  -o jsonpath='{range .items[*]}  gateway {.status.containerStatuses[0].imageID}{"\n"}{end}'
echo
echo "rollback complete"
