# 2026-09-08 mlops-postgres disk-full crash + ingress TLS mismatch

Severity: prod incident. Database primary crash-looped, operator failed over,
litellm gateway pods OOMKilled twice in the preceding days, public endpoint
served a hostname-mismatched TLS certificate. Fully recovered same day; the
underlying unbounded-growth cause was fixed the same day; secondary findings
(gateway memory creep, alerting gaps) were investigated and mitigated over the
following day.

Status: RESOLVED. Retention verified deleting rows. Monitor live in prod.

Plain-language version for non-technical readers:
[executive summary](executive-summary.md)

---

## Executive summary

Three unrelated configuration defects combined into one bad day:

1. **LiteLLM spend logs grew unbounded for 75 days** (no retention configured)
   until the 50Gi Postgres data PVC filled to ~98%. Postgres could not write
   WAL/checkpoints; the primary crash-looped; the CNPG operator failed over.
   Retention is now enabled (60d) and verified deleting.
2. **The ingress referenced TLS secrets that never existed**, so ingress-nginx
   silently served its internal default certificate for both public hosts —
   a hostname mismatch for strict-verify clients. Fixed by pointing at the
   existing DigiCert wildcard secret.
3. **No monitoring warned anyone**: the kube-prometheus-stack's
   `KubePersistentVolumeFillingUp` alert evaluates `kubelet_volume_stats_*`,
   which does not exist on kubelet v1.34 (removed from `/metrics`) — so the
   PVC alert silently had no data — and the Alertmanager config routes
   critical/warning severities to receivers that are never defined. The
   gateway is now decoupled from the database (survives any DB failure), so
   automated DB-size alerting was closed as unnecessary; the developer
   monitors usage via the memory monitor's Loki logs.

Additionally, the gateway pods themselves had a separate memory-growth problem
(OOMKilled twice in the week before the DB incident), investigated and
mitigated separately (see "Gateway OOM" section).

---

## Full timeline (all times UTC, 2026-09-08 unless noted)

Reconstructed from CNPG operator logs, postgres container logs, k8s events,
and LiteLLM proxy logs. Post-fix events verified live on 2026-09-09.

### Background (weeks before)

| When | Event |
|---|---|
| 2025-12-13 | `mlops-postgres` CNPG cluster created (Helm release `oicm`); data PVC 50Gi, WAL PVC 50Gi |
| 2026-06-25 | litellm DB created (first SpendLogs row 2026-06-25 13:49); from day one **no `maximum_spend_logs_retention_period`** was configured, so LiteLLM never scheduled its built-in cleanup job — every request appended a row forever |
| 2026-06-25 → 09-08 | `LiteLLM_SpendLogs` grew to **48 GB / 13.2M rows in 75 days** (~176K rows/day average, accelerating to ~500K/day by September ≈ 1.7–2 GB/day incl. indexes+TOAST at recent rates). 72% of each row's bytes is `metadata`, 72% of *that* is `model_map_information` — a ~5 KB verbatim copy of the model's registry entry written per row (117 fields, 95% null), write-only, never read back (see Row Anatomy) |
| 2026-09-05 03:58 | litellm-proxy pod `wrgvs` **OOMKilled** (exit 137, 8Gi limit, RSS 6.5–6.7Gi). First proxy crash of the week — separate from the DB problem. Evidence: `containerStatuses.lastState` captured live 09-08 (pod replaced since; k8s events expire). Restart verified in Loki: granian banner 03:58:52 UTC |
| 2026-09-07 07:42 | litellm-proxy pod `mjt5r` **OOMKilled** again (same pattern). Two kills in 3 days at 8Gi. Restart verified in Loki: granian banner 07:42:51 UTC. The database was healthy all day (zero DB-unreachable lines Sep 5–7), so this kill was steady-state accumulation, not DB-related |

### The DB failure (morning of 09-08)

| Time | Event |
|---|---|
| 05:23 | First disk-full errors in Postgres logs: `could not write to file "base/pgsql_tmp/...": No space left on device` (parallel workers on the primary, `mlops-postgres-1` at the time). Errors continue through 06:40 and spread to replicas 2/3 by 07:18 (`pg_logical/replorigin_checkpoint.tmp` PANIC-class writes). The database degrades progressively for ~2.5h before the operator notices |
| 07:34 | Gateway (litellm-proxy) starts logging P1001 `Can't reach database server at mlops-postgres-rw.mlops:5432` — the app-visible start of the outage |
| 07:47 | Postgres starts refusing connections: `the database system is not yet accepting connections` |
| 07:56–08:09 | CNPG operator churns: repeated `Creating new Pod to reattach a PVC`. Instances cannot come up healthy — every start attempt hits the full disk |
| 08:03–08:09 | Operator attempts the right self-heal: PVC expansion to match the cluster spec. **Longhorn admission webhook `CheckReplicasSizeExpansion` DENIES it** — repeated `error while changing PVC storage requirement: cannot schedule 53687091200 more bytes to disk 3cad3033... / 82d84ac7...` (storage-01/disk-3 and storage-02/disk-4 both over their bookkeeping cap; see Longhorn section). Operator logs show the same rejection every ~5s |
| 08:38 | Primary still down. Clients (the litellm gateway) see `FATAL: the database system is not yet accepting connections`; instance-manager logs `Instance is still down, will retry in 1 second` |
| 08:38:53 | Operator gives up on -3: `Current primary isn't healthy, initiating a failover` from `mlops-postgres-3` → `mlops-postgres-1` (replication timeline 3 → 4) |
| 08:38:58 | Postgres on -3: `archive command failed with exit code 1` — CNPG's wal-archive **refusing** with `switchover in progress` (benign; a red herring, not disk-related) |
| 08:39–08:45 | **Manual intervention** (see Fix sequence): Longhorn over-provisioning raised 100→150, unblocking the webhook; cluster spec patched to 100Gi; operator expands PVCs in place (`resizeInUseVolumes: true`) |
| 08:40:43 | Operator logs `Cluster has become healthy`. 3/3 instances, primary `mlops-postgres-1`, phase "Cluster in healthy state" |
| 08:45 | End-to-end verified: gateway serving, DB writes succeeding |
| 10:23 | **Both gateway pods restart ~1.5h AFTER the DB recovered** (`wrgvs` 10:23:08, `mjt5r` 10:23:40 — container restarts, same pods). The P1001 storm ran 07:34–08:38 and the pods kept logging through it, so the liveness-probe/OOM kill did not land during the outage window itself; the sustained churn during the outage is the plausible stressor and the pods died under its after-effects. `mjt5r` restarted a second time at 10:38:58 |

### The gateway during the outage (why requests kept working while pods died)

The gateway does **not** need Postgres to serve requests — verified: warm-cache
auth (`enable_redis_auth_cache: true`, TTL 3600) serves from Redis; master-key
traffic skips DB lookups entirely. What died was the pods, via this chain:

```
PG disk-full → primary down → every DB call raises P1001 (prisma mislabels it;
  rescued by keyword match in exception_handler.py:127-168)
  → 4 granian workers × per-tick rebuild of 10k-row flush batches + MB-scale
    JSON serialization, retried and requeued every 2-30s forever (no hot spin,
    but continuous heavy work)                      utils.py:6301-6420
  → auth cache misses each pay a ~2s reconnect budget
    before failing                                  auth_checks.py:3132-3175
  → reconnect watchdog escalates to "heavy reconnect":
    a NEW prisma query-engine subprocess spawned/killed
    every ~15s of continued failure, per worker     utils.py:3492, 5067-5150
  → event loop saturates → /health/liveliness (which
    touches NOTHING - no DB, no Redis, no locks) times
    out → kubelet restarts the pod → OOMKilled 137
```

The liveness probe failing on a trivial endpoint is the smoking gun: pure
event-loop starvation plus RSS ratcheting to the 8Gi limit under failed-GC
pressure, not DB coupling.

Timing note (verified against Loki): the gateway logged P1001 errors
continuously 07:34–08:38 and stayed up through the outage; the actual pod
restarts landed at 10:23 (both pods) with a second `mjt5r` restart at 10:38,
roughly 1.5h after the DB recovered. The kill did not land inside the outage
window — treat this chain as the stressor that accumulated during it, with
the deaths trailing after recovery.

### The ingress TLS problem (present since 2026-06-25, found same day)

| When | Event |
|---|---|
| 2026-06-25 | Ingress applied referencing secrets `litellm.ecouncil.ae-tls` and `litellm.adeoaiengine.ecouncil.ae-tls` (dot-named) — **neither was ever created** |
| since then | ingress-nginx logged `SSL certificate ... was not found. Using default certificate` and served the controller's default internal cert (`*.adeoaiengine.ecouncil.ae`, EC-ISSUINGCA) for BOTH hosts. Strict-verify clients to `litellm.ecouncil.ae` got hostname mismatch (curl `ssl_verify_result=20`, HTTP 000) |
| ~08:50 | Found during verification; fixed same session |

### Resolution & verification (same day → 09-09)

| When | Event |
|---|---|
| ~08:40–08:45 | DB recovered (see Fix sequence) |
| ~08:50 | Ingress fixed (commit `b0d766c44c`): both hosts → existing `litellm-ecouncil-ae-tls` (DigiCert `*.ecouncil.ae`, expires 2027-01-14). Verified: `litellm.ecouncil.ae` serves DigiCert, HTTP 200, ssl_verify OK |
| same day | 60d retention enabled (commit `d46df2176c`): `general_settings.maximum_spend_logs_retention_period: "60d"` in litellm-proxy.yaml, applied live, pods restarted (ConfigMap mounts need restart) |
| same day | Gateway-DB decoupling (commit `c5182135b4`): `allow_requests_on_db_unavailable: true`, retention drain capacity raised (interval 6h, 2000 batches/run, 20m budget — 500K → 8M rows/day drain) |
| same day | `mlops-postgres-cluster.yaml` mirrored into deploy/prod (commit `93ebda45d1`) so a future Helm upgrade of release `oicm` cannot revert 100Gi → 50Gi |
| evening | Prod memory limit 8Gi → 12Gi + `memory_monitor_job` shipped (commit `df73dfe33f`): mem_monitor lines now in prod logs and Loki |
| 2026-09-08 23:42 | **First retention cleanup run executed** (pod start 17:42 + 6h interval) |
| 2026-09-09 06:34 | **Retention VERIFIED WORKING**: rows older than 60d = **0** (was 200,724 at deploy); `n_dead_tup` = 217,289 (deletes ran, autovacuum in progress); PVCs 51/99G and stable; 0 restarts since 12Gi deploy |
| 2026-09-09 ~11:30 | Re-verified live while writing this report: rows past 60d still 0; oldest surviving row 2026-07-12 01:53; table 50 GB / 13.46M rows; dead tuples 223K (autovacuum cycling with the 0.02 scale factor) |

---

## Root cause (primary): unbounded spend-log growth

`LiteLLM_SpendLogs` = 48 GB of the 49 GB database:

- 13,205,736 rows spanning exactly 2026-06-25 13:49 → 2026-09-08 (75 days),
  measured 09-08 before any deletion; after retention went live the oldest
  surviving row is 2026-07-12 01:53 (a rolling 60-day window)
- ~3.6 KB/row all-in (heap + TOAST + index share); measured row anatomy:
  - `metadata` JSONB: 2,833 B/row avg — and **72% of that is
    `model_map_information`**: a ~5,241 B verbatim copy of the model's
    registry entry (117 fields, 95% null; only key/mode/provider/costs/rpm
    filled), identical for every request of the same model, write-only —
    no code ever reads it back from the DB (verified by exhaustive grep)
  - index overhead: 4.5 GB across 6 indexes (pkey, startTime,
    startTime+request_id, session_id, end_user, model_group+startTime)
- No retention: without `maximum_spend_logs_retention_period` in
  `general_settings`, LiteLLM never registers its cleanup job. Nothing
  deleted a single row (n_dead_tup was 0 before the incident = zero deletes
  ever ran)

Storage math: nothing in the stack (Longhorn, Kubernetes, CNPG) auto-grows a
volume. All three layers require a manual trigger. The volume filled; Postgres
could not write; the primary went down. The equation:
`storage = 3.6 KB × rows`, and rows were unbounded.

**Contributing factor**: the row is fat because of `model_map_information`
(~60% of heap bytes). Kept as-is (upstream parity decision — it has exactly
one commit in BerriAI history, adding it in Apr 2025, never touched since;
zero GitHub issues mention the bloat). Documented as an upstream-PR candidate.

## Root cause (secondary): why nothing alerted

- `kubelet_volume_stats_*` metrics **do not exist on kubelet v1.34** (removed
  from `/metrics`; verified: 0 series while kubelet targets are `up`). The
  stock `KubePersistentVolumeFillingUp` alert has been silently evaluating
  empty data — it could never fire on this cluster
- The Alertmanager config routes `severity=critical`/`warning` to receivers
  named `critical`/`warning` that **are never defined** — every alert in the
  cluster reaches nobody
- Working metric identified for future alerts:
  `cnpg_pg_database_size_bytes{datname="litellm"}` (CNPG exporter, already
  scraped; verified 54.6 GB live on 09-09)

## Why Longhorn blocked the resize (the webhook error)

The admission webhook enforces per-disk:
`storageScheduled ≤ storageMaximum × overProvisioningPercentage/100`.
Scheduling counts each replica's *requested* size, not physical usage. At the
old 100% limit:

Measured at incident time (09-08); re-measured 09-09 (values drift as
scheduling changes):

```
adeo-storage-01/disk-3   scheduled=36.35TB  max=30.72TB  118%  <- disk in error (UUID 3cad3033); 09-09: 37.10TB, 121%
adeo-storage-03/disk-9   scheduled=36.26TB  max=30.72TB  118%  (09-09: 36.46TB, 119%)
adeo-storage-02/disk-4   scheduled=30.65TB  max=30.72TB  100%  (webhook also named this disk, UUID 82d84ac7)
adeo-storage-02/disk-5   scheduled=30.28TB  max=30.72TB  99%   (09-09: 30.28TB, 99%)
```

Physical free space was healthy (15–21 TB per storage disk) — the constraint
was bookkeeping, not capacity. Raising
`settings.longhorn.io/storage-over-provisioning-percentage` 100 → 150 lifted
the ceiling to 46 TB/disk and admitted the +50Gi. Tradeoff: physical
exhaustion is now possible if usage catches up to scheduled; monitor real
free space on storage nodes (`/var/lib/longhorn`).

## Fix sequence (in order applied)

1. `kubectl patch cluster mlops-postgres -n mlops --type='json' -p='[{"op": "replace", "path": "/spec/storage/size", "value": "100Gi"}]'` — the correct fix. CNPG `resizeInUseVolumes: true` expands the PVCs itself. Direct PVC patches for -1/-2 returned "no change" because the operator had already done them
2. `kubectl patch settings.longhorn.io storage-over-provisioning-percentage -n longhorn-system --type='merge' -p '{"value": "150"}'` — unblocked the webhook (was rejecting the operator's expansion attempts at 08:03–08:09)
3. Operator-managed failover promoted `mlops-postgres-1` (no manual promotion)
4. Ingress TLS fix (commit `b0d766c44c`)
5. Retention + decoupling + manifest mirror (commits `d46df2176c`, `c5182135b4`, `93ebda45d1`)
6. Memory limit 12Gi + memory monitor (commit `df73dfe33f`)

## Solutions

Every issue above was resolved the same day. The full resolution details —
sizing rationale, row anatomy, the OOM mechanism analysis, the gateway-DB
decoupling design, and the memory creep investigation — live in the
companion document: [resolutions.md](resolutions.md).

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
kubectl logs -n mlops mlops-postgres-3 -c postgres --previous --tail=800 | \
  python3 -c "import sys,json
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
kubectl logs -n mlops mlops-postgres-3 -c postgres --previous --tail=800 \
  | grep 'failed to run wal-archive' | head -2
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

### PVC resize outcome + retention verification

```bash
# All data PVCs should show 100Gi/100Gi after the fix
kubectl get pvc -n mlops -l cnpg.io/cluster=mlops-postgres \
  -o custom-columns='NAME:.metadata.name,CAP:.status.capacity.storage,REQ:.spec.resources.requests.storage,ROLE:.metadata.labels.cnpg\.io/pvcRole'

# Retention working? past-60d count should be ~0 (was 200,724 at deploy),
# and n_dead_tup > 0 proves deletes ran
kubectl exec -n mlops mlops-postgres-1 -c postgres -- psql -U postgres -d litellm -c \
  "SELECT COUNT(*) FILTER (WHERE \"startTime\" < NOW() - INTERVAL '60 days') AS past_retention,
          COUNT(*) AS total FROM \"LiteLLM_SpendLogs\";
   SELECT n_dead_tup FROM pg_stat_user_tables WHERE relname = 'LiteLLM_SpendLogs';"
```

### Memory monitor (post-fix, live in prod)

```bash
# Structured samples in container logs -> Loki
POD=$(kubectl get pods -n mlops -l app=litellm-proxy -o jsonpath='{.items[0].metadata.name}')
kubectl logs -n mlops $POD --tail=2000 | grep "mem_monitor"
# Loki query: {job="fluent-bit", namespace_name="mlops", pod_name=~"litellm-proxy.*"} |= "mem_monitor"
# (fluent-bit's grafana-loki output labels the k8s fields namespace_name/pod_name,
# and the tenant header X-Scope-OrgID: oiai-loki-logs is required)
# Watch: rss_mb (current, fluctuates), prom_series (cardinality growth?),
# delta_mb (per-sample growth). Linear climb in both = metric cardinality leak.

# NOTE: the mem_monitor INFO lines require the monitor's own logger
# ("LiteLLM Proxy MemoryMonitor", level INFO) — the proxy logger itself is
# NOTSET by default (only --detailed_debug raises it), so its INFO would be
# filtered by the root logger's WARNING.
```

### Cluster audit log (enabled, on masters; not reachable from workstation)

```bash
# kube-apiserver runs with: --audit-log-path=/var/log/kubernetes/audit/audit.log
# --audit-log-maxage=30 --audit-policy-file=/etc/kubernetes/audit-policy.yaml
# From a bastion / master:
grep mlops-postgres /var/log/kubernetes/audit/audit.log \
  | jq -r 'select(.verb=="patch" or .verb=="update" or .verb=="create") | [.stageTimestamp,.verb,.objectRef.resource,.objectRef.name,.user.username] | @tsv'
```

## Resolution commits (branch jya0-v1.99.1)

| Commit | Change |
|---|---|
| `b0d766c44c` | Ingress TLS → existing DigiCert wildcard secret |
| `93ebda45d1` | Mirror mlops-postgres cluster spec into deploy/prod |
| `d46df2176c` | Enable 60d spend-log retention (live) |
| `c5182135b4` | Gateway-DB decoupling + cleanup drain capacity (6h interval, 2M rows/run, 20m budget) |
| `df73dfe33f` | memory_monitor job + current-RSS fix + prod limit 12Gi |
| `9196765454` | Dev gateway DB-less validation |
| `dfa3b4ae87` | Dev-only postgres for profiling |
| `9a3dfdafae` | memray profiling rig (INSTALL_PROFILERS image) |
| `a7841c9bb5` | Dev discovery controller read-write (24 models into dev DB) |
| `33798c230b` | memory monitor logger visibility fix |

## Follow-ups

All resolution details, verification data, and open items live in the
companion document: [resolutions.md](resolutions.md).

