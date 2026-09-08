# 2026-09-08 mlops-postgres disk-full crash + ingress TLS mismatch

Severity: prod incident. DB primary crash-looped, operator failed over, litellm
gateway ran degraded TLS. Recovered same day.

---

## Timeline (all times UTC, reconstructed from CNPG operator + postgres logs)

| Time | Event |
|---|---|
| 2026-06-25 | litellm DB created; `LiteLLM_SpendLogs` starts growing, no retention configured |
| 2026-09-07 / 09-05 | litellm-proxy pods OOMKilled (exit 137) twice — separate issue, 8Gi limit, running 6.5–6.7Gi |
| 09-08 07:56–08:09 | CNPG operator repeatedly recreated pods: "Creating new Pod to reattach a PVC"; instances could not come up healthy (data PVC ~98% full) |
| 09-08 08:03–08:05+ | Operator tried PVC expansion, Longhorn admission webhook DENIED it (see Longhorn section) |
| 09-08 08:38 | primary `mlops-postgres-3` still down: postgres `FATAL: the database system is not yet accepting connections`, instance-manager `Instance is still down, will retry in 1 second` |
| 09-08 08:38:53 | Operator: "Current primary isn't healthy, initiating a failover" 3 → 1 (timeline 3 → 4) |
| 09-08 08:38:58 | postgres on -3: `archive command failed with exit code 1` — CNPG wal-archive refusing with "switchover in progress" (benign, not disk) |
| 09-08 ~08:40 | storage-over-provisioning-percentage raised 100 → 150; PVC expansion admitted; all data PVCs at 100Gi |
| 09-08 08:45 | Cluster healthy: Ready=True, 3/3 instances, primary mlops-postgres-1 |
| 09-08 ~08:50 | Ingress TLS secret reference fixed (commit b0d766c44c) |

## Root cause

`LiteLLM_SpendLogs` = 48 GB of the 49 GB database (13.2M rows, 75 days of
traffic, ~500–650K rows/day ≈ 2 GB/day). The data PVC was 50Gi (since cluster
creation 268d ago) and filled to ~98%. Nothing anywhere auto-grows a Longhorn
volume — Postgres could no longer write WAL/checkpoints and the primary went
down. No `maximum_spend_logs_retention_period` was configured, so LiteLLM never
even scheduled its built-in cleanup job.

Next-largest table: LiteLLM_ModelPerformanceRollup at 266 MB. The spend table
is the entire problem.

## Why Longhorn blocked the resize (the webhook error)

The admission webhook enforces per-disk: `storageScheduled ≤ storageMaximum ×
overProvisioningPercentage/100`. Scheduling counts each replica's *requested*
size, not physical usage. At the old 100% limit:

    adeo-storage-01/disk-3   scheduled=36.35TB  max=30.72TB  118%  <- disk in error (UUID 3cad3033)
    adeo-storage-03/disk-10  scheduled=36.26TB  max=30.72TB  118%
    adeo-storage-02/disk-5,6 scheduled=30.45/30.28TB          99%

Physical free space was healthy (15–21 TB per storage disk) — the constraint
was bookkeeping, not capacity. Raising
`settings.longhorn.io/storage-over-provisioning-percentage` 100 → 150 lifted
the ceiling to 46 TB/disk and admitted the +50Gi. Tradeoff: physical
exhaustion is now possible if usage catches up to scheduled; monitor real free
space on storage nodes.

## What was used to fix it (in order)

1. `kubectl patch cluster mlops-postgres -n mlops --type='json' -p='[{"op": "replace", "path": "/spec/storage/size", "value": "100Gi"}]'` — the correct fix. CNPG `resizeInUseVolumes: true` expands the PVCs itself. Direct PVC patches for -1/-2 returned "no change" because the operator had already done them
2. `kubectl patch settings.longhorn.io storage-over-provisioning-percentage -n longhorn-system --type='merge' -p '{"value": "150"}'` — unblocked the webhook that was rejecting PVC expansion
3. Operator-managed failover promoted mlops-postgres-1; no manual promotion
4. `mlops-postgres-cluster.yaml` mirrored into deploy/prod (commit 93ebda45d1) so a future helm upgrade of release `oicm` cannot revert 100Gi → 50Gi

## Ingress TLS discrepancy (what was wrong vs what is now running)

- Manifest referenced secrets `litellm.ecouncil.ae-tls` and
  `litellm.adeoaiengine.ecouncil.ae-tls` (dot-named). Neither exists in mlops.
  ingress-nginx logged `SSL certificate ... was not found. Using default
  certificate` and silently served its default internal cert
  (`*.adeoaiengine.ecouncil.ae`, EC-ISSUINGCA) for BOTH hosts
- Strict-verify clients to `litellm.ecouncil.ae` got hostname mismatch (curl
  ssl_verify_result=20, HTTP 000)
- Fix (commit b0d766c44c): point both hosts at the existing
  `litellm-ecouncil-ae-tls` secret (DigiCert `*.ecouncil.ae`, expires
  2027-01-14). Now `litellm.ecouncil.ae` serves the DigiCert cert (HTTP 200,
  verify OK)
- Caveat: `litellm.adeoaiengine.ecouncil.ae` still serves the internal default
  cert — a wildcard matches ONE label, `*.ecouncil.ae` cannot cover a
  2-label subdomain; the controller log confirms it rejects the cert for that
  host. Internal clients trusting the ADEO CA are unaffected. External strict
  clients need that host added as a SAN on a DigiCert cert
- The dot-named secrets were never created; nothing cert-manager managed
  (no Certificate objects). The one TLS secret that exists was created
  manually 2026-08-19

## Evidence: exact commands

### Confirm root cause — what filled the disk

```bash
# Database sizes (litellm = 49 GB)
kubectl exec -n mlops mlops-postgres-1 -c postgres -- \
  psql -U postgres -c "SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database ORDER BY 2 DESC;"

# Top tables (LiteLLM_SpendLogs = 48 GB)
kubectl exec -n mlops mlops-postgres-1 -c postgres -- \
  psql -U postgres -d litellm -c \
  "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) FROM pg_catalog.pg_statio_user_tables ORDER BY 2 DESC LIMIT 6;"

# Row count + date span of the spend logs
kubectl exec -n mlops mlops-postgres-1 -c postgres -- \
  psql -U postgres -d litellm -c \
  'SELECT COUNT(*) FROM "LiteLLM_SpendLogs";' \
  -c 'SELECT MIN("startTime"), MAX("startTime") FROM "LiteLLM_SpendLogs";'

# Retention configured? (empty result = never ran cleanup)
kubectl get deploy litellm-proxy -n mlops -o jsonpath='{range .spec.template.spec.containers[0].env[*]}{.name}={.value}{"\n"}{end}' | grep -iE 'spend|retain'
```

### CNPG operator decision log (failover, PVC errors)

```bash
POD=$(kubectl get pods -n cnpg -o name | grep operator | head -1)

# failover / promotion decisions
kubectl logs -n cnpg $POD --since=48h --tail=50000 | grep '"namespace":"mlops"' \
  | grep -iE 'failover|elect|promotion'

# Longhorn webhook rejections of PVC expansion
kubectl logs -n cnpg $POD --since=48h --tail=80000 | grep '"namespace":"mlops"' \
  | grep -iE 'error while changing PVC storage requirement' | head -3

# pod reattach churn during the outage
kubectl logs -n cnpg $POD --since=48h --tail=80000 | grep '"namespace":"mlops"' \
  | grep -iE 'reattach'
```

### Postgres-level crash evidence

```bash
# FATAL / ERROR records from the crashed container
kubectl logs -n mlops mlops-postgres-3 -c postgres --previous --tail=800 | python3 -c "
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line.startswith('{'):
        continue
    try:
        rec = json.loads(line)
    except Exception:
        continue
    r = rec.get('record') or {}
    if r.get('error_severity') in ('FATAL', 'ERROR', 'PANIC'):
        print(rec.get('ts', '')[:19], r.get('error_severity'), r.get('message', '')[:130])"

# wal-archive failures during switchover (benign)
kubectl logs -n mlops mlops-postgres-3 -c postgres --previous --tail=800 | grep 'failed to run wal-archive' | head -2
```

### Kubernetes events

```bash
kubectl get events -n mlops --sort-by=.lastTimestamp | grep -E 'postgres'
# key lines: FailingOver from mlops-postgres-3, FailoverTarget mlops-postgres-1,
# BackOff restarting failed container postgres, Startup/Readiness probe 500s
```

### Longhorn over-provisioning state (the webhook math)

```bash
kubectl get settings.longhorn.io -n longhorn-system \
  storage-over-provisioning-percentage storage-minimal-available-percentage

kubectl get nodes.longhorn.io -n longhorn-system -o json | python3 -c "
import json, sys
for node in json.load(sys.stdin)['items']:
    for did, d in node.get('status', {}).get('diskStatus', {}).items():
        sched = int(d.get('storageScheduled', 0)); mx = int(d.get('storageMaximum', 0))
        if mx and sched / mx > 0.9:
            print(node['metadata']['name'], d.get('diskName'), f'{sched / mx * 100:.0f}% of max')"

# The exact rejection error (webhook name + disk UUID) — CAREFUL: this actually
# resizes if the webhook allows it. Only run to re-observe the error, then
# revert: kubectl patch pvc mlops-postgres-3 -n mlops -p '{"spec":{"resources":{"requests":{"storage":"100Gi"}}}}'
kubectl patch pvc mlops-postgres-3 -n mlops -p '{"spec":{"resources":{"requests":{"storage":"110Gi"}}}}'
```

### Ingress TLS verification

```bash
# Which cert each host actually serves
echo | openssl s_client -connect litellm.ecouncil.ae:443 -servername litellm.ecouncil.ae 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates
echo | openssl s_client -connect litellm.adeoaiengine.ecouncil.ae:443 -servername litellm.adeoaiengine.ecouncil.ae 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates

# End-to-end with strict verification
curl -s -o /dev/null -w "HTTP %{http_code} ssl_verify=%{ssl_verify_result}\n" https://litellm.ecouncil.ae/health/liveliness

# Controller's fallback warnings
kubectl logs -n nginx-ingress-controller -l app.kubernetes.io/name=nginx-ingress-controller --tail=200 \
  | grep -iE 'not found|default certificate|does not contain a Common Name'
```

### PVC resize outcome

```bash
kubectl get pvc -n mlops -l cnpg.io/cluster=mlops-postgres \
  -o custom-columns='NAME:.metadata.name,CAP:.status.capacity.storage,REQ:.spec.resources.requests.storage,ROLE:.metadata.labels.cnpg\.io/pvcRole'
```

### Cluster audit log (enabled, on masters; not reachable from workstation)

```bash
# kube-apiserver runs with: --audit-log-path=/var/log/kubernetes/audit/audit.log
# --audit-log-maxage=30 --audit-policy-file=/etc/kubernetes/audit-policy.yaml
# From a bastion / master:
grep mlops-postgres /var/log/kubernetes/audit/audit.log | jq -r 'select(.verb=="patch" or .verb=="update" or .verb=="create") | [.stageTimestamp,.verb,.objectRef.resource,.objectRef.name,.user.username] | @tsv'
```

## Follow-ups (open)

1. Enable spend log retention — the actual fix:
   `general_settings: maximum_spend_logs_retention_period: "30d"` (plus
   `disable_error_logs: True` per LiteLLM production best practices) in
   litellm-proxy.yaml + live. Bounded cleanup: ~500K rows/run, safe. At 30d,
   ~3.4M rows become prunable. Disk returns only after VACUUM FULL (or move to
   range partitioning via db_scripts/partition_spend_logs.sql — drops
   partitions instantly, recommended at this volume)
2. Add Prometheus alert at 80% PVC usage:
   `kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes > 0.8`
   (kube-prometheus-stack is already installed)
3. Investigate litellm-proxy OOMKills (exit 137, 8Gi limit, 6.5–6.7Gi steady)
4. Consider `litellm.adeoaiengine.ecouncil.ae` SAN on a DigiCert cert if
   external strict-TLS clients use that host
5. Watch real free space on Longhorn storage nodes now that
   over-provisioning is 150% (currently 15–21 TB free per disk — fine)
