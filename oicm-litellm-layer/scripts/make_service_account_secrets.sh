#!/usr/bin/env bash
# Generate the Secrets the OICM service-account provisioning Jobs mount.
#
# This is the only step that cannot be a plain manifest: it generates a random
# password and copies a cluster's Keycloak admin credentials, neither of which
# belongs in git. Everything downstream (the Jobs, the ConfigMaps, the
# controller's env wiring) is declarative.
#
# Usage:
#   # Al Ain account, provisioned in and for Al Ain
#   KUBECONFIG=$HOME/.kube/alain-oicm.conf \
#     oicm-litellm-layer/scripts/make_service_account_secrets.sh alain
#
#   # Abu Dhabi account. The credential is consumed by the Al Ain controller, so
#   # it is written into Al Ain, while the admin password is read from Abu Dhabi
#   # through the gateway-node relay and passed in (that cluster's API server is
#   # not reachable from this workstation):
#   ADMIN_PASSWORD=... KUBECONFIG=$HOME/.kube/alain-oicm.conf \
#     oicm-litellm-layer/scripts/make_service_account_secrets.sh abudhabi
#   # or let the script read it itself, when run on a host with Abu Dhabi access:
#   KUBECONFIG=$HOME/.kube/alain-oicm.conf \
#     ADMIN_KUBECONFIG=$HOME/.kube/abudhabi-kubeconfig.yaml \
#     oicm-litellm-layer/scripts/make_service_account_secrets.sh abudhabi
#
# Re-running rotates the service-account password, so follow with the matching
# provisioning Job.
set -euo pipefail

CLUSTER="${1:-}"
TARGET_NS="adeo-litellm"

case "$CLUSTER" in
  alain)
    ADMIN_NS="oicm-keycloak"
    ADMIN_SECRET="oicm-keycloak-keycloak-admin-credentials"
    TARGET_SECRET="oicm-status-api"
    ADMIN_TARGET_SECRET="oicm-keycloak-admin"
    ;;
  abudhabi)
    ADMIN_NS="keycloak"
    ADMIN_SECRET="keycloak-secret"
    TARGET_SECRET="ad-oicm-status-api"
    ADMIN_TARGET_SECRET="ad-oicm-keycloak-admin"
    ;;
  *)
    echo "usage: $0 {alain|abudhabi}" >&2
    exit 2
    ;;
esac

command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }

# Reading admin credentials may need a different kubeconfig than writing the
# Secrets. They are the same for Al Ain; for Abu Dhabi the read needs that
# cluster while the write goes to Al Ain.
ADMIN_KUBECTL="kubectl"
if [ -n "${ADMIN_KUBECONFIG:-}" ]; then
  ADMIN_KUBECTL="kubectl --kubeconfig=$ADMIN_KUBECONFIG"
fi

echo "cluster      : $CLUSTER"
echo "admin source : $ADMIN_NS/$ADMIN_SECRET"
echo "target       : $TARGET_NS/$TARGET_SECRET and $TARGET_NS/$ADMIN_TARGET_SECRET"

# ADMIN_USERNAME / ADMIN_PASSWORD override the cluster read. Needed for Abu
# Dhabi, whose API server is reachable only from the Al Ain gateway node, so
# the password has to be read through the relay there and passed in.
ADMIN_USER="${ADMIN_USERNAME:-}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"

if [ -z "$ADMIN_PASSWORD" ]; then
  ADMIN_USER=$($ADMIN_KUBECTL -n "$ADMIN_NS" get secret "$ADMIN_SECRET" \
    -o jsonpath='{.data.admin-username}' 2>/dev/null | base64 -d || true)
  ADMIN_PASSWORD=$($ADMIN_KUBECTL -n "$ADMIN_NS" get secret "$ADMIN_SECRET" \
    -o jsonpath='{.data.admin-password}' 2>/dev/null | base64 -d || true)
fi

# Abu Dhabi's keycloak-secret carries only admin-password; its user is admin.
if [ -z "$ADMIN_USER" ]; then
  ADMIN_USER="admin"
fi
if [ -z "$ADMIN_PASSWORD" ]; then
  echo "could not read admin-password from $ADMIN_NS/$ADMIN_SECRET" >&2
  echo "pass ADMIN_PASSWORD=... (and ADMIN_USERNAME=... if not 'admin')" >&2
  exit 1
fi

SA_PASSWORD=$(openssl rand -base64 24 | tr -d '/+=' | head -c 28)

kubectl -n "$TARGET_NS" create secret generic "$ADMIN_TARGET_SECRET" \
  --from-literal=admin-username="$ADMIN_USER" \
  --from-literal=admin-password="$ADMIN_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$TARGET_NS" create secret generic "$TARGET_SECRET" \
  --from-literal=username=svc-litellm-controller \
  --from-literal=password="$SA_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

echo
echo "wrote $TARGET_NS/$TARGET_SECRET and $TARGET_NS/$ADMIN_TARGET_SECRET"
if [ "$CLUSTER" = "alain" ]; then
  echo "next: make oicm-sa-provision"
else
  echo "next: make oicm-sa-provision-abudhabi"
fi
