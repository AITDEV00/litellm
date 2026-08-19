#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Create a kubernetes.io/tls secret from a .pfx certificate, generalized
# for any namespace / secret name.
#
# Generalization of create-tls-secret.sh (which was hardcoded to the
# 'ecas' namespace / 'ecas-tls' secret) for the adeoaiengine cluster.
#
# Traefik and ingress-nginx both choke on PKCS#12 bag attributes, so we
# strip every PEM block down to clean BEGIN/END markers before creating
# the secret. That is the same critical gotcha handled in the original.
#
# Usage:
#   ./create-tls-secret-ns.sh <path-to-cert.pfx> <pfx-password> <namespace> <secret-name>
#
# Example (DigiCert *.ecouncil.ae -> litellm.ecouncil.ae in mlops):
#   ./create-tls-secret-ns.sh 'ecouncil.ae-30062026-inter 1.pfx' \
#     'Adeo@234' mlops litellm-ecouncil-ae-tls
#
# Prerequisites: kubectl (configured for the target cluster), openssl.
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

PFX_FILE="${1:?Usage: $0 <path-to-cert.pfx> <pfx-password> <namespace> <secret-name>}"
PFX_PASS="${2:?Usage: $0 <path-to-cert.pfx> <pfx-password> <namespace> <secret-name>}"
NAMESPACE="${3:?Usage: $0 <path-to-cert.pfx> <pfx-password> <namespace> <secret-name>}"
SECRET_NAME="${4:?Usage: $0 <path-to-cert.pfx> <pfx-password> <namespace> <secret-name>}"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

echo ">>> Extracting certificate and private key from $PFX_FILE..."

# Extract private key (unencrypted, -nodes)
openssl pkcs12 -in "$PFX_FILE" -nocerts -nodes -passin "pass:$PFX_PASS" \
  -out "$TMPDIR/privkey-raw.pem"

# Extract certificate chain
openssl pkcs12 -in "$PFX_FILE" -nokeys -passin "pass:$PFX_PASS" \
  -out "$TMPDIR/fullchain-raw.pem"

# Strip PKCS#12 bag attributes — Traefik/nginx cannot parse them and
# silently fall back to their default cert.
awk '/-----BEGIN PRIVATE KEY-----/,/-----END PRIVATE KEY-----/' \
  "$TMPDIR/privkey-raw.pem" > "$TMPDIR/privkey.pem"
awk 'BEGIN{p=0} /-----BEGIN CERTIFICATE-----/{p=1} p{print} /-----END CERTIFICATE-----/{p=0}' \
  "$TMPDIR/fullchain-raw.pem" > "$TMPDIR/fullchain.pem"

# Validate
openssl rsa -in "$TMPDIR/privkey.pem" -check -noout
openssl x509 -in "$TMPDIR/fullchain.pem" -noout -subject -issuer -dates -ext subjectAltName

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
echo "Done. nginx picks up the new cert automatically (no restart needed)."
echo "Verify with:"
echo "  echo | openssl s_client -connect litellm.ecouncil.ae:443 \\"
echo "    -servername litellm.ecouncil.ae 2>/dev/null | \\"
echo "    openssl x509 -noout -subject -issuer -dates"