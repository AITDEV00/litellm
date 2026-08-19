#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# ECAS — Create the ecas-tls Kubernetes secret from a .pfx certificate.
#
# The ecas-tls secret is referenced by all ingresses for ecas-dev.ecouncil.ae
# (ecas-vite-ingress, ecas-keycloak-ingress, ecas-docmgmt-ingress). Traefik
# reads tls.crt + tls.key from it to terminate TLS on the websecure entrypoint.
#
# Usage:
#   ./k8s/create-tls-secret.sh <path-to-cert.pfx> <pfx-password>
#
# Example:
#   ./k8s/create-tls-secret.sh ~/ecouncil.ae-30062026-inter.pfx 'Adeo@234'
#
# The script extracts the cert chain and private key from the .pfx, strips
# PKCS#12 bag attributes (Traefik requires clean PEM), and creates/replaces
# the ecas-tls secret in the ecas namespace.
#
# Prerequisites: kubectl (configured for the target cluster), openssl.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

PFX_FILE="${1:?Usage: $0 <path-to-cert.pfx> <pfx-password>}"
PFX_PASS="${2:?Usage: $0 <path-to-cert.pfx> <pfx-password>}"
NAMESPACE="ecas"
SECRET_NAME="ecas-tls"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

echo ">>> Extracting certificate and private key from $PFX_FILE..."

# Extract private key (unencrypted, -nodes)
openssl pkcs12 -in "$PFX_FILE" -nocerts -nodes -passin "pass:$PFX_PASS" \
  -out "$TMPDIR/privkey-raw.pem"

# Extract certificate chain
openssl pkcs12 -in "$PFX_FILE" -nokeys -passin "pass:$PFX_PASS" \
  -out "$TMPDIR/fullchain-raw.pem"

# Strip PKCS#12 bag attributes — Traefik cannot parse them and silently
# falls back to its default self-signed cert.
awk '/-----BEGIN PRIVATE KEY-----/,/-----END PRIVATE KEY-----/' \
  "$TMPDIR/privkey-raw.pem" > "$TMPDIR/privkey.pem"
awk 'BEGIN{p=0} /-----BEGIN CERTIFICATE-----/{p=1} p{print} /-----END CERTIFICATE-----/{p=0}' \
  "$TMPDIR/fullchain-raw.pem" > "$TMPDIR/fullchain.pem"

# Validate
openssl rsa -in "$TMPDIR/privkey.pem" -check -noout
openssl x509 -in "$TMPDIR/fullchain.pem" -noout -subject -issuer -dates

# Verify key matches cert
CERT_MOD=$(openssl x509 -in "$TMPDIR/fullchain.pem" -noout -modulus | openssl md5)
KEY_MOD=$(openssl rsa -in "$TMPDIR/privkey.pem" -noout -modulus | openssl md5)
if [ "$CERT_MOD" != "$KEY_MOD" ]; then
  echo "ERROR: Certificate and private key do not match." >&2
  exit 1
fi
echo ">>> Cert and key verified — they match."

# Create or replace the secret
echo ">>> Creating $SECRET_NAME secret in $NAMESPACE namespace..."
kubectl create secret tls "$SECRET_NAME" \
  --cert="$TMPDIR/fullchain.pem" \
  --key="$TMPDIR/privkey.pem" \
  -n "$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -

echo ">>> Verifying..."
kubectl get secret "$SECRET_NAME" -n "$NAMESPACE"

echo ""
echo "✅ Done. Traefik picks up the new cert automatically (no restart needed)."
echo "   Verify with:"
echo "   echo | openssl s_client -connect ecas-dev.ecouncil.ae:443 \\"
echo "     -servername ecas-dev.ecouncil.ae 2>/dev/null | \\"
echo "     openssl x509 -noout -subject -issuer -dates"
