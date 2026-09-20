# Guideline: Apply `*.ecouncil.ae` Wildcard Certificate to a Domain

> **Stack:** Kubernetes (DKP) + Traefik ingress (`kommander-traefik`).
> **Cert:** DigiCert wildcard `*.ecouncil.ae`, distributed as a `.pfx` file.
> Covers **every** `xxx.ecouncil.ae` subdomain — same `.pfx` for all.

This is the procedure we used to fix `ecas-dev.ecouncil.ae` on 2026-08-12,
generalized for any domain / namespace / VM running the same K8s + Traefik
stack.

---

## Prerequisites

| What | Detail |
|---|---|
| `.pfx` file | The wildcard cert bundle, e.g. `~/ecouncil.ae-30062026-inter.pfx` |
| `.pfx` password | The export password, e.g. `Adeo@234` |
| `kubectl` | Configured for the target cluster (`kubectl get ns` works) |
| `openssl` | For extraction and verification |
| The script | `k8s/apply-wildcard-cert.sh` in this repo |

---

## Step 1 — Identify the target namespace, ingress, and secret name

Find which namespace serves the domain and what TLS secret name the ingress
expects:

```bash
# List all ingresses with their hosts and TLS secret names
kubectl get ingress -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.spec.rules[*].host}{"\t"}{.spec.tls[*].secretName}{"\n"}{end}'
```

Sample output:
```
ecas    ecas-vite-ingress     ecas-dev.ecouncil.ae    ecas-tls
gsip    gsip-ingress          gsip-dev.ecouncil.ae   gsip-tls
mdm     mdm                   mdm-dev.ecouncil.ae    mdm-dev-tls
```

Write down:
- **NAMESPACE** — e.g. `gsip`
- **SECRET_NAME** — e.g. `gsip-tls` (the name the ingress references)
- **DOMAIN** — e.g. `gsip-dev.ecouncil.ae`

> **If the TLS column is empty**, the ingress has no TLS section. You need to
> add one (see Step 4 below) or the ingress won't serve HTTPS at all.

---

## Step 2 — Check for conflicting cert-manager `Certificate` CRs

**This is the trap that caused the ecas-dev blank-page bug.** If a
cert-manager `Certificate` resource exists for the same domain, Traefik may
serve *that* self-signed cert instead of yours. Check before applying:

```bash
# List all cert-manager Certificate CRs
kubectl get certificate -A -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,SECRET:.spec.secretRef.name,ISSUER:.spec.issuerRef.name,DNS:.spec.dnsNames
```

Look for any `Certificate` whose `DNS` matches your target domain. If you find
one:

```bash
# Inspect it
kubectl get certificate <name> -n <namespace> -o yaml

# Delete it so it stops fighting your real cert
kubectl delete certificate <name> -n <namespace>
```

Also check if the secret it created still lingers:
```bash
kubectl get secret <secret-from-cert> -n <namespace>
# If it exists and differs from your target secret name, delete it:
kubectl delete secret <secret-from-cert> -n <namespace>
```

**Rule of thumb:** for a given domain, there should be exactly ONE source of
the TLS secret — either the manual `kubernetes.io/tls` secret you're about to
create, OR a cert-manager `Certificate`. Never both.

---

## Step 3 — Apply the certificate

Run the generalized script with four arguments: `<pfx-file> <pfx-password>
<namespace> <secret-name>`:

```bash
cd ~/ecas-frontend   # or wherever the repo is cloned

./k8s/apply-wildcard-cert.sh \
  ~/ecouncil.ae-30062026-inter.pfx \
  'Adeo@234' \
  <NAMESPACE> \
  <SECRET_NAME>
```

**What the script does:**
1. Extracts private key + cert chain from the `.pfx` via `openssl pkcs12`.
2. **Strips PKCS#12 bag attributes with `awk`** — *critical gotcha*. Traefik
   cannot parse bag attributes and silently falls back to its default
   self-signed cert. The script extracts only the PEM blocks between
   `BEGIN`/`END` markers.
3. Validates that the key modulus matches the cert modulus.
4. Creates/replaces the `kubernetes.io/tls` secret via `kubectl apply`.

Traefik picks up the new cert **automatically** — no restart needed.

---

## Step 4 — (Only if the ingress has no TLS section) Add one

If Step 1 showed an empty TLS column, patch the ingress to reference your
secret:

```bash
kubectl patch ingress <ingress-name> -n <namespace> --type=json -p='[
  {"op":"add","path":"/spec/tls","value":[
    {"hosts":["<domain>"],"secretName":"<secret-name>"}
  ]}
]'
```

Or edit directly:
```bash
kubectl edit ingress <ingress-name> -n <namespace>
# Add under spec:
#   tls:
#     - hosts:
#         - <domain>
#       secretName: <secret-name>
```

---

## Step 5 — Verify

### 5a. Check the served cert (should be DigiCert, not self-signed)

```bash
echo | openssl s_client -connect <domain>:443 \
  -servername <domain> 2>/dev/null | \
  openssl x509 -noout -subject -issuer -dates
```

Expected output:
```
subject=CN = *.ecouncil.ae, ...
issuer=C = US, O = DigiCert Inc, ...
notBefore=...
notAfter=Jan 14 04:59:59 2027 GMT
```

If you see an empty subject or a self-signed issuer, see Troubleshooting below.

### 5b. Check HTTPS returns 200 (no `-k` flag)

```bash
curl -sI https://<domain>/ | head -5
```

Should return `HTTP/2 200` or `HTTP/1.1 200` or a redirect — **not** an SSL
error.

### 5c. Confirm the secret exists in the namespace

```bash
kubectl get secret <secret-name> -n <namespace>
# TYPE should be kubernetes.io/tls
```

---

## Troubleshooting

### Symptom: Browser shows cert error / `curl` fails with "self-signed certificate"

| Likely cause | Fix |
|---|---|
| PKCS#12 bag attributes not stripped | Use the script — it strips them. If you did it manually, re-extract with the `awk` commands. |
| Conflicting cert-manager `Certificate` CR for same domain | Step 2 — delete the `Certificate` and any secret it created. |
| Secret created in wrong namespace | Re-run the script with the correct namespace. |
| Ingress doesn't reference the secret | Step 4 — add/fix the TLS section. |

### Symptom: HTTP doesn't redirect to HTTPS

Traefik's global entrypoint redirect handles this automatically
(`--entrypoints.web.http.redirections.entryPoint.to=:443`). If it's not
working, the redirect config is cluster-level — check the Traefik deployment
args, not your ingress.

### Symptom: Cert applied but still serving old/wrong cert

Traefik caches certs. Wait 30 seconds, or check if another ingress in a
**different namespace** has a TLS section for the same host (Traefik may pick
the wrong one). This is exactly what happened with `ecas-resolution-export`
in `ecas-apps` namespace:

```bash
# Find ALL ingresses across ALL namespaces serving the same host
kubectl get ingress -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.spec.rules[*].host}{"\t"}{.spec.tls[*].secretName}{"\n"}{end}' | grep <domain>
```

If another namespace has a competing TLS config, remove its TLS section:
```bash
kubectl patch ingress <competing-ingress> -n <competing-namespace> --type=json -p='[
  {"op":"remove","path":"/spec/tls"}
]'
```

---

## Renewal

The current cert expires **Jan 14, 2027**. When you receive a new `.pfx`:

1. Copy the new `.pfx` to the VM.
2. Re-run the same script for each namespace/secret that needs updating:
   ```bash
   ./k8s/apply-wildcard-cert.sh ~/new-cert.pfx 'new-password' <namespace> <secret-name>
   ```
3. Verify with Step 5.

The script uses `kubectl apply` (not `create`), so it overwrites the existing
secret in place. No need to delete first.

---

## Quick reference — all known domains on this cluster

| Domain | Namespace | Secret name | Status |
|---|---|---|---|
| `ecas-dev.ecouncil.ae` | `ecas` | `ecas-tls` | ✅ Applied (2026-08-12) |
| `ecas-dev.ecouncil.ae` | `ecas-apps` | *(ecas-resolution-export — TLS removed)* | ⚠️ Conflicting ingress — TLS section removed to prevent cert conflict |
| `agents-dev.ecouncil.ae` | `ecas-apps` | `committee-api-tls` | Uses cert-manager self-signed — may need real cert |
| `gsip-dev.ecouncil.ae` | `gsip` | `gsip-tls` | cert-manager self-signed — may need real cert |
| `mdm-dev.ecouncil.ae` | `mdm` | `mdm-dev-tls` | cert-manager self-signed — may need real cert |
| `advisor.ecouncil.ae` | `adeo-advisor-app` | `adeo-ui-tls` | Check if real or self-signed |
| `stg-gpt.ecouncil.ae` | `adeo-gpt-stg` | `stg-gpt-tls` | Check if real or self-signed |
| `fpu-dev.ecouncil.ae` | `fpu-engine` | `fpu-engine-tls` | Check if real or self-signed |
| `fpu-mvp-dev.ecouncil.ae` | `fpu-engine` | `fpu-engine-tls` | Same secret as fpu-dev |

To apply the real wildcard cert to any of the "self-signed" domains above:
1. Delete the cert-manager `Certificate` CR for that domain (Step 2).
2. Delete the old self-signed secret if the name differs from what the ingress
   references, or just overwrite it with the script using the same secret name.
3. Run the script with the namespace and secret name from the table.
