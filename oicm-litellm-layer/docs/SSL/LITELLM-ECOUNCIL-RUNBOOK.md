# Runbook: Serve `litellm.ecouncil.ae` with the DigiCert Wildcard

> **Stack:** Kubernetes (ingress-nginx, `k8s.io/ingress-nginx`) on the
> `adeoaiengine` cluster. **Not** Traefik — see `CERT-GUIDELINE.md` for the
> DKP/Traefik flavor (that one targets `ecas`/`gsip`/`mdm`).
> **Cert:** DigiCert wildcard `*.ecouncil.ae`, distributed as a `.pfx`.
>
> Applied **2026-08-19** to the `litellm-proxy` ingress in the `mlops`
> namespace.

---

## Why this was done

`litellm.adeoaiengine.ecouncil.ae` was the existing host, but its TLS secret
(`litellm.adeoaiengine.ecouncil.ae-tls`) **did not exist**, so nginx silently
fell back to the cluster default certificate (the internal `EC-ISSUINGCA`
wildcard `*.adeoaiengine.ecouncil.ae`). That cert is only trusted by
domain-joined ADEO machines, not by public clients / BYOD / Python-`curl`.

Two candidate fixes were ruled out:

- Adding `*.adeoaiengine.ecouncil.ae` as a SAN to the existing DigiCert cert:
  **not possible.** DigiCert only allows *non-wildcard* SANs on a wildcard
  order, and a Standard/wildcard SKU may not accept SANs at all. Also
  `litellm.adeoaiengine.ecouncil.ae` is two levels deep, so even the wildcard
  would not match it.
- Let's Encrypt via cert-manager (DNS-01): **blocked by egress.** The cluster
  has no outbound internet (`acme-v02.api.letsencrypt.org` returned
  `Network is unreachable` from a pod), so ACME is unreachable.

The chosen solution: expose litellm under a **single-level** hostname
`litellm.ecouncil.ae`, which the existing DigiCert `*.ecouncil.ae` wildcard
already covers directly. No DigiCert reissue or new purchase needed.

---

## Topology before / after

```
BEFORE
litellm.adeoaiengine.ecouncil.ae  (ingress rule)
   tls.secretName = litellm.adeoaiengine.ecouncil.ae-tls  (MISSING)
   -> nginx falls back to default-cert = internal EC-ISSUINGCA wildcard
      (trusted only by domain-joined machines)

AFTER
litellm.adeoaiengine.ecouncil.ae  (ingress rule)  -> (unchanged, still fallback)
litellm.ecouncil.ae               (ingress rule)  -> tls.secretName = litellm-ecouncil-ae-tls
                                                   -> DigiCert *.ecouncil.ae  (publicly trusted)
```

---

## Step 1 — Create the DigiCert TLS secret

Extract the key + chain from the `.pfx`, strip PKCS#12 bag attributes
(nginx/Traefik both fail to parse them and silently fall back to their
default cert), validate, then create the secret.

Use the generalized script in this folder:

```bash
cd oicm-litellm-layer/docs/SSL

./create-tls-secret-ns.sh \
  'ecouncil.ae-30062026-inter 1.pfx' \
  'Adeo@234' \
  mlops \
  litellm-ecouncil-ae-tls
```

Expected output (abridged):

```
RSA key ok
subject=... CN=ecouncil.ae
issuer=... CN=DigiCert Global G2 TLS RSA SHA256 2020 CA1
notAfter=Jan 14 23:59:59 2027 GMT
X509v3 Subject Alternative Name:
    DNS:ecouncil.ae, DNS:*.ecouncil.ae
secret/litellm-ecouncil-ae-tls created
```

If you prefer to run it manually, the equivalent steps are:

```bash
TMP=$(mktemp -d)
# key + chain
openssl pkcs12 -in 'ecouncil.ae-30062026-inter 1.pfx' -nocerts -nodes -passin pass:'Adeo@234' -out "$TMP/key.pem"
openssl pkcs12 -in 'ecouncil.ae-30062026-inter 1.pfx' -nokeys    -passin pass:'Adeo@234' -out "$TMP/chain.pem"
# strip bag attributes
awk '/-----BEGIN PRIVATE KEY-----/,/-----END PRIVATE KEY-----/' "$TMP/key.pem"   > "$TMP/privkey.pem"
awk 'BEGIN{p=0} /-----BEGIN CERTIFICATE-----/{p=1} p{print} /-----END CERTIFICATE-----/{p=0}' "$TMP/chain.pem" > "$TMP/fullchain.pem"
# create
kubectl create secret tls litellm-ecouncil-ae-tls \
  --cert="$TMP/fullchain.pem" --key="$TMP/privkey.pem" -n mlops \
  --dry-run=client -o yaml | kubectl apply -f -
rm -rf "$TMP"
```

> The secret is named `litellm-ecouncil-ae-tls` (a *new* name) — the old
> `.adeoaiengine...` name was left untouched because that host is unchanged.

---

## Step 2 — Point the ingress at the secret

The `litellm-proxy` ingress in `mlops` originally had one rule (the old host)
and one TLS entry referencing the missing secret. We **added** the new host and
a second TLS entry; nothing existing was removed.

```bash
kubectl patch ingress litellm-proxy -n mlops --type=json -p='[
  {"op":"add","path":"/spec/rules/-","value":{"host":"litellm.ecouncil.ae","http":{"paths":[{"path":"/","pathType":"Prefix","backend":{"service":{"name":"litellm-proxy","port":{"number":4000}}}}]}}},
  {"op":"add","path":"/spec/tls/-","value":{"hosts":["litellm.ecouncil.ae"],"secretName":"litellm-ecouncil-ae-tls"}}
]'
```

Expected result:

```bash
kubectl get ingress litellm-proxy -n mlops -o jsonpath='{range .spec.rules[*]}{.host}{"\n"}{end}'
# litellm.adeoaiengine.ecouncil.ae
# litellm.ecouncil.ae

kubectl get ingress litellm-proxy -n mlops -o jsonpath='{range .spec.tls[*]}{.hosts[0]} -> {.secretName}{"\n"}{end}'
# litellm.adeoaiengine.ecouncil.ae -> litellm.adeoaiengine.ecouncil.ae-tls
# litellm.ecouncil.ae              -> litellm-ecouncil-ae-tls
```

nginx picks the new secret up automatically via SNI — no reload needed.

---

## Step 3 — DNS (the remaining step, external)

The cluster has **no outbound internet**, so DNS is handled by the org's
internal DNS, not by the cluster. A public / internal A record is required
for the hostname to reach the ingress load balancer at all.

```text
litellm.ecouncil.ae   A    <ingress load balancer IP>
```

The ingress load balancer IP is:

```bash
kubectl get ingress litellm-proxy -n mlops -o jsonpath='{.status.loadBalancer.ingress[0].ip}{"\n"}'
```

It should match the IP that the existing host resolves to
(`litellm.adeoaiengine.ecouncil.ae`). File a network ticket with that IP.

---

## Step 4 — Verify

**A. Check the served cert (must be DigiCert, not self-signed):**

```bash
echo | openssl s_client -connect litellm.ecouncil.ae:443 \
  -servername litellm.ecouncil.ae 2>/dev/null | \
  openssl x509 -noout -subject -issuer -dates -ext subjectAltName
```

Expected:
```
subject=... CN=ecouncil.ae
issuer=C=US, O=DigiCert Inc, CN=DigiCert Global G2 TLS RSA SHA256 2020 CA1
notAfter=Jan 14 23:59:59 2027 GMT
```

**B. HTTPS returns 200 (no `-k`):**

```bash
curl -sI https://litellm.ecouncil.ae/ | head -5
```

**C. Confirm the secret:**

```bash
kubectl get secret litellm-ecouncil-ae-tls -n mlops
# TYPE should be kubernetes.io/tls, DATA 2
```

---

## Troubleshooting

### Browser shows cert error even though the host matches

- **Incomplete chain.** The secret must contain the leaf **and** the DigiCert
  intermediate (2 certs). Verify with:
  ```bash
  kubectl get secret litellm-ecouncil-ae-tls -n mlops -o jsonpath='{.data.tls\.crt}' | base64 -d | grep -c "BEGIN CERTIFICATE"
  # should be 2
  ```
- **PKCS#12 bag attributes not stripped**: re-run the script; the `awk` steps
  are what remove them.

**`litellm.ecouncil.ae` doesn't resolve (Name or service not known):**
The DNS A record hasn't been created yet. Confirm with
`getent hosts litellm.ecouncil.ae`; the DNS team needs to add the A record.

**Old host still shows internal cert:** expected — we did not change the old
host's TLS secret. It still falls back to the default. If you want the old host
to also use a real cert, add a `kubernetes.io/tls` secret for it (or reuse the
DigiCert secret) and point its TLS entry at it. Or drop the old host once
nothing depends on it.

**Other ingresses serving the same host elsewhere:** nginx will pick one
TLS binding per host. If a different namespace has an ingress rule for
`litellm.ecouncil.ae`, remove its TLS section so it doesn't compete:
```bash
kubectl get ingress -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\t"}{.spec.rules[*].host}{"\t"}{.spec.tls[*].secretName}{"\n"}{end}' | grep litellm.ecouncil.ae
```

---

## Renewal

The DigiCert cert expires **Jan 14, 2027**. When the new `.pfx` arrives:
1. Re-run Step 1 (`create-tls-secret-ns.sh`) with the same namespace + secret
   name — it uses `kubectl apply`, so it overwrites in place.
2. No ingress change needed (it already points at the secret name).
3. Verify with Step 4.

No outbound internet is involved in applying the secret, so this works even
though the cluster is egress-restricted.

---

## Files / artifacts

| Artifact | Purpose |
|---|---|
| `create-tls-secret-ns.sh` | Generalized pfx->tls-secret script (namespace/secret params) |
| `ecouncil.ae-30062026-inter 1.pfx` | The DigiCert `*.ecouncil.ae` bundle (password-protected) |
| `CERT-GUIDELINE.md` | Original Traefik/DKP guide (ecas/gsip/mdm) |
| Ingress `litellm-proxy` (mlops) | Updated with `litellm.ecouncil.ae` + TLS entry |
| Secret `litellm-ecouncil-ae-tls` (mlops) | DigiCert leaf + intermediate |

---

## What still needs the human

- **DNS A record** `litellm.ecouncil.ae -> <LB IP>` (network ticket, external).
- Decide whether to also fix/drop the old `litellm.adeoaiengine.ecouncil.ae`
  host (currently still on the fallback cert).