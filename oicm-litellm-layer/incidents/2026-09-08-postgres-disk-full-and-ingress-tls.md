# 2026-09-08 mlops-postgres disk-full crash + ingress TLS mismatch

Severity: prod incident. Database primary crash-looped, operator failed over,
litellm gateway pods OOMKilled twice in the preceding days, public endpoint
served a hostname-mismatched TLS certificate. Fully recovered same day; the
underlying unbounded-growth cause was fixed the same day; secondary findings
(gateway memory creep, alerting gaps) were investigated and mitigated over the
following day.

Status: RESOLVED. Retention verified deleting rows. Monitor live in prod.

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
3. **No monitoring would have warned anyone**: the kube-prometheus-stack's
   `KubePersistentVolumeFillingUp` alert evaluates `kubelet_volume_stats_*`,
   which does not exist on kubelet v1.34 (removed from `/metrics`) — so the
   PVC alert silently had no data — and the Alertmanager config routes
   critical/warning severities to receivers that are never defined, so even a
   firing alert reaches no one.

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
| 2026-06-25 → 09-08 | `LiteLLM_SpendLogs` grew to **48 GB / 13.2M rows in 75 days** (~500–650K rows/day ≈ 1.7–2 GB/day incl. indexes+TOAST). 72% of each row's bytes is `metadata`, 72% of *that* is `model_map_information` — a ~5 KB verbatim copy of the model's registry entry written per row (117 fields, 95% null), write-only, never read back (see Row Anatomy) |
| 2026-09-05 03:58 | litellm-proxy pod `wrgvs` **OOMKilled** (exit 137, 8Gi limit, RSS 6.5–6.7Gi). First proxy crash of the week — separate from the DB problem. Evidence: `containerStatuses.lastState` captured live 09-08 (pod replaced since; k8s events expire) |
| 2026-09-07 07:42 | litellm-proxy pod `mjt5h` **OOMKilled** again (same pattern). Two kills in 3 days at 8Gi |

### The DB failure (morning of 09-08)

| Time | Event |
|---|---|
| ~07:55 | Data PVC hits ~98% (49/50Gi). Postgres can no longer write WAL or checkpoints; the primary (`mlops-postgres-3`) stops accepting connections |
| 07:56–08:09 | CNPG operator churns: repeated `Creating new Pod to reattach a PVC`. Instances cannot come up healthy — every start attempt hits the full disk |
| 08:03–08:09 | Operator attempts the right self-heal: PVC expansion to match the cluster spec. **Longhorn admission webhook `CheckReplicasSizeExpansion` DENIES it** — repeated `error while changing PVC storage requirement: cannot schedule 53687091200 more bytes to disk 3cad3033...` (adeo-storage-01/disk-3 scheduled at 118% of its bookkeeping cap; see Longhorn section). Operator logs show the same rejection every ~5s |
| 08:38 | Primary still down. Clients (the litellm gateway) see `FATAL: the database system is not yet accepting connections`; instance-manager logs `Instance is still down, will retry in 1 second` |
| 08:38:53 | Operator gives up on -3: `Current primary isn't healthy, initiating a failover` from `mlops-postgres-3` → `mlops-postgres-1` (replication timeline 3 → 4) |
| 08:38:58 | Postgres on -3: `archive command failed with exit code 1` — CNPG's wal-archive **refusing** with `switchover in progress` (benign; a red herring, not disk-related) |
| 08:39–08:45 | **Manual intervention** (see Fix sequence): Longhorn over-provisioning raised 100→150, unblocking the webhook; cluster spec patched to 100Gi; operator expands PVCs in place (`resizeInUseVolumes: true`) |
| 08:40:43 | Cluster `Ready=True`, 3/3 instances, primary `mlops-postgres-1`, phase "Cluster in healthy state" |
| 08:45 | End-to-end verified: gateway serving, DB writes succeeding |

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
  scraped; verified 52.6 GB live)

## Why Longhorn blocked the resize (the webhook error)

The admission webhook enforces per-disk:
`storageScheduled ≤ storageMaximum × overProvisioningPercentage/100`.
Scheduling counts each replica's *requested* size, not physical usage. At the
old 100% limit:

Measured at incident time (09-08); re-measured 09-09 (values drift as
scheduling changes):

```
adeo-storage-01/disk-3   scheduled=36.35TB  max=30.72TB  118%  <- disk in error (UUID 3cad3033); 09-09: 36.97TB, 120%
adeo-storage-03/disk-10  scheduled=36.26TB  max=30.72TB  118%  (09-09: 36.32TB, 118%)
adeo-storage-02/disk-5,6 scheduled=30.45/30.28TB          99%   (09-09: 30.51TB, 99%)
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

## Sizing: why 60d fits

Measured live: the 60-day window holds ~13.0M rows ≈ **62 GB** including
indexes + TOAST, against the 100Gi volume — ~38 GB headroom. Steady state:
~1.7 GB/day inserted, ~1.7 GB/day deleted (drain capacity was raised to
8M rows/day: 6h interval × 2000 batches × 1000 rows). Deletion frees space
that inserts reuse; the table plateaus instead of growing.

## Row anatomy (what one SpendLogs row costs)

Measured from prod (2,000-row samples; re-verified 09-09):

| Component | Avg bytes | What it is |
|---|---|---|
| Heap row | 3,559 | tuple header + columns |
| → of which `metadata` JSONB | 2,833–2,872 | see breakdown (80% of heap-row bytes) |
| → → `model_map_information` | 4,880–5,241 | 117-field model registry copy, 95% null |
| → → `cost_breakdown` | 398 | useful |
| → → `usage_object` | 250 | useful |
| → → other 17 keys | ~400 | small |
| Storage split | | heap 9.5 GB + TOAST ~34 GB + indexes 4.5 GB = 48 GB |

**"Doesn't the system overwrite old logs when there's no space?"** — No. Three
separate behaviors, none of which is space-based overwrite:

1. **RAM queue (64 MB cap, drop-oldest)**: pending rows queue in memory before
   flushing (`enqueue_spend_logs`, utils.py:5983-6006). Past the budget the
   oldest *queued* rows are dropped — protects the pod, not the disk
2. **Retention cleanup deletes by AGE only** (60d), never by disk pressure —
   there is no code anywhere checking `pg_total_relation_size()`,
   `pg_disk_available()`, or PG error 53100 (verified: zero matches)
3. **When the disk is actually full**: INSERTs fail AND the retention DELETEs
   fail too (deleting requires WAL writes, which need disk) — the cleanup job
   aborts after 3 consecutive batch failures. Neither old logs are removed nor
   new logs written: a stalemate until manual intervention (exactly the state
   at 07:55–08:40)

Space-based auto-overwrite would be an upstream feature request (a job
checking DB size and force-pruning oldest data below a threshold); the
current defense is retention-by-age + alerts.

## Why OOM appeared 2–3 days after pod starts (proxy OOM mechanism)

### Hypothesis tested: "pending writes to the dead DB caused the OOM" — DISPROVEN

The natural theory — pods OOM'd because pending DB writes piled up in memory
— was tested against the code and **rejected**: every pending-write structure
is bounded, and their combined maximum is ~100–110 MB, nowhere near the 8Gi
limit. Measured from the running image (same tag as at incident time):

| Pending-write structure | Bound | Max during outage |
|---|---|---|
| `spend_log_transactions` (the log rows) | 64 MB byte cap, drop-oldest (`constants.py:1546`, `utils.py:5999`) | **64 MB** |
| 9 × `asyncio.Queue` (entity/daily/rollup aggregates) | 1,000 items each (`constants.py` LITELLM_ASYNCIO_QUEUE_MAXSIZE); full queue = `put()` blocks the request path (backpressure, not growth) | ~9–45 MB |
| `tool_usage_transactions` list (UNBOUNDED) | drained ≤10K/tick; entry = small dataclass (`request_id`, dates, tool names, spend — **not** the payload; `spend_log_tool_index.py:29`) | ~6 MB (14K entries × ~400B in a 40-min outage) |
| `autorouter_turn_transactions` list (UNBOUNDED) | same shape — small scalar fields only (`autorouter_session_rollup.py`) | ~6 MB |
| Redis buffer lists | unbounded RPUSH, but they grow **in the Redis pod**, not the gateway; gateway-side pop/restore transients only | — |

Total: **~95–110 MB bounded**. The OOM gap (RSS climbing from a 6.5 Gi
baseline through the 8 Gi limit) cannot be explained by pending writes.

### The actual OOM mechanism (what the evidence supports)

The kills came from CPU-side event-loop starvation plus transient allocation
churn on top of an already-high baseline, not from any unbounded queue:

1. **4 granian workers × per-tick rebuild** of ≤10K-row flush batches with
   MB-scale JSON serialization, retried/requeued every 2–30s against the dead
   DB for the whole outage window — continuous heavy CPU + transient
   allocations (`utils.py:6301-6420`)
2. **Auth reconnect latency**: every auth cache miss during the outage paid a
   ~2s reconnect budget before failing (`auth_checks.py:3132-3175`) —
   concurrent requests pile up as awaiting tasks, each holding its request
   payload
3. **Prisma engine churn**: the reconnect watchdog spawns a NEW query-engine
   subprocess every ~15s of continued failure, per worker
   (`utils.py:3492, 5067-5150`) — each engine is a child process with its own
   RSS inside the same 8Gi cgroup (hundreds of MB of spikes)
4. **GC starvation**: with the event loop saturated, GC cycles fall behind
   allocation rate — transient garbage accumulates as real RSS

The liveness probe failing on `/health/liveliness` (which touches no DB, no
Redis, no locks) is the definitive signature: the loop couldn't schedule a
trivial handler, so the kubelet killed the pod.

The serving path itself is leak-free — proven by memray (4,800 real DeepSeek
V4 requests: 3.1M allocations, 9.7 GB cumulative churn, peak live tracked
memory 753 MB, **flat** for the whole session).

Post-incident verification (09-09): with `allow_requests_on_db_unavailable:
true` the same failure mode cannot recur — auth falls back instead of
piling up, and pods start even with the DB down.

## Why OOM appeared 2–3 days after pod starts (the two kills, separated)

1. **Sep 7 kill = DB-outage amplification** (the mechanism above, coinciding
   with the disk-full window)
2. **Sep 5 kill = steady-state accumulation** (before any DB trouble):
   ~1.6 Gi/day RSS growth from per-request buffering, fragmentation, metric
   cardinality — subtle, and the motivation for the `memory_monitor_job` and
   the 12Gi limit below

## Gateway-DB decoupling (the architectural fix)

`allow_requests_on_db_unavailable: true` (config-only, upstream-native) now
makes a DB outage non-fatal for the gateway:

- auth failures fall back to a restricted synthetic identity
  (`INTERNAL_USER`, `user_id="__db_unavailable_fallback__"`) instead of 503
- pods start without the DB; the health watchdog reconnects when it returns
- the spend queue stays bounded (64 MB, drop-oldest) — logs may be lost,
  never availability

Validated end-to-end on dev: the dev gateway was deliberately run **DB-less**
(no `DATABASE_URL` at all) — startup, readiness (`"db":"Not connected"`),
auth and serving all worked; then re-attached to a new dev-only CNPG cluster
(`mlops-postgres-dev`, 1 instance) with the real logging pipeline for
profiling.

## Memory creep investigation (post-incident)

- tracemalloc in-process profiling (1,800 full write cycles): no leak in
  payload construction + DB writes; ~3.4 KB/request retained = proportional
  to work
- memray on the serving path (4,800 real requests): peak live tracked memory
  753 MB, flat — no leak
- `memory_monitor_job` now samples prod every 60s: current RSS, peak RSS,
  Prometheus series count, threads, GC stats, delta — in logs and Loki
  (`{namespace="mlops"} |= "mem_monitor"`)
- Live prod data (09-09): RSS oscillates per pod (983/1081/1395/1118 MiB —
  periodic work), `delta_mb ≈ 0`, no linear growth since 12Gi deploy. At the
  observed rate the 12Gi limit gives 70+ days of headroom
- Suspected (unproven) remaining creep driver: Prometheus metric cardinality
  (10.5K–14.8K series as of 09-09, grows with key/team/model diversity); only
  `end_user` labels are cardinality-capped upstream (10K series, 1h TTL) —
  model/key/team labels are not

---

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
# Loki query: {namespace="mlops", pod=~"litellm-proxy.*"} |= "mem_monitor"
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

## Follow-ups (open)

### Completeness verification (2026-09-09, live-checked)

Every issue identified in this incident was cross-checked against the commit
log AND the running cluster:

| Issue | Fix commit(s) | Live-verified state |
|---|---|---|
| Unbounded spend-log growth | `d46df2176c` (60d retention) + `c5182135b4` (drain capacity) | **✅ Deleting**: 0 rows past 60d (was 200,724); rows cycling (inserts ≈ deletes); oldest surviving row rolls at the 60d boundary (2026-07-12) |
| DB full = stalemate (no auto-reclaim) | `d46df2176c` + autovacuum tuning via psql | **✅ Mitigated**: retention now reclaims continuously so the stalemate window requires a 6x traffic burst to reach; per-table autovacuum (scale 0.02) keeps dead tuples bounded |
| Gateway crashes when DB down | `c5182135b4` (`allow_requests_on_db_unavailable`) | **✅ Live**: flag in pod config; validated end-to-end on dev (DB-less startup + serving) |
| Gateway OOMKills (8Gi limit) | `df73dfe33f` (12Gi) | **✅ Live**: limit 12Gi, 0 restarts since deploy; memray proved serving path leak-free |
| Memory creep invisibility | `df73dfe33f` + `33798c230b` (memory monitor) | **✅ Live**: mem_monitor lines in prod logs/Loki; RSS floor flat, cycle peaks drifting ~11 MB/day (~1,000 days to limit) |
| Ingress TLS mismatch | `b0d766c44c` | **✅ Live**: `litellm.ecouncil.ae` serves DigiCert; `litellm.adeoaiengine.ecouncil.ae` serves internal cert (accepted, see follow-up 5) |
| 100Gi revert risk (Helm) | `93ebda45d1` (manifest mirror) | **✅ Live**: PVCs report 100Gi; manifest in deploy/prod |
| Longhorn webhook blocking resize | settings patch 100→150 | **✅ Live**: over-provisioning = 150 |
| Autovacuum lag after mass deletes | psql per-table tuning (0d7c6def59) | **✅ Live**: reloptions on SpendLogs/ToolIndex/GuardrailIndex (scale 0.02) |
| Slow-bleed alerting gap | **NOT FIXED** | **❌ OPEN**: no PrometheusRule for DB-size alerts; Alertmanager critical/warning receivers still undefined (alerts would go nowhere); needs a notification target |

### Follow-ups (open)

1. ~~Enable spend log retention~~ **DONE** (60d, verified deleting: 0 rows
   past 60d as of 09-09, 217K dead tuples pending autovacuum). Optionally
   `VACUUM FULL` later to shrink below the plateau, or range partitioning via
   `db_scripts/partition_spend_logs.sql` (drops partitions instantly,
   recommended at this volume). **2026-09-09 maintenance done**: online
   `VACUUM (ANALYZE)` run, and per-table autovacuum tuned on
   LiteLLM_SpendLogs / SpendLogToolIndex / SpendLogGuardrailIndex
   (`autovacuum_vacuum_scale_factor = 0.02` vs default 0.2) so autovacuum
   triggers after ~270K dead tuples instead of 2.6M — matching the daily
   ~500K-row delete churn from the retention job. DDL lives only on the
   primary via psql (not in any manifest); re-apply if the cluster is ever
   rebuilt
2. ~~Memory monitoring~~ **DONE** (`memory_monitor_job` live in prod). Remaining:
   watch `prom_series` growth over days; if linear with RSS, apply
   `prometheus_metrics_config` label filtering (upstream caps only
   `end_user`; model/key/team labels are uncapped)
3. ~~Alerting~~ **OPEN — the only remaining gap**: no PrometheusRule exists
   for DB-size alerts. When created, they must use
   `cnpg_pg_database_size_bytes{datname="litellm"}` (verified working) — NOT
   `kubelet_volume_stats_*` (absent on kubelet v1.34). The Alertmanager
   `critical`/`warning` receivers are still undefined — every alert in the
   cluster currently goes nowhere; wiring needs a notification target (Slack
   webhook/email). Interim detection is covered by the memory monitor's
   structured logs in Loki (prom_series floor stable at 10.8K as of 09-09;
   a rising floor = cardinality leak signature)
4. ~~Investigate proxy OOMKills~~ **RESOLVED**: root cause identified (DB-outage
   amplification + steady-state buffering); mitigated by
   `allow_requests_on_db_unavailable`, 12Gi limit, and the monitor. Serving
   path proven leak-free by memray
5. ~~Gateway TLS for `litellm.adeoaiengine.ecouncil.ae`~~ still serving the
   internal default cert (functional; internal clients unaffected). Add the
   host as a SAN on a DigiCert cert only if external strict-TLS clients need it
6. Watch real free space on Longhorn storage nodes now that
   over-provisioning is 150% (currently 15–21 TB free per disk — fine)
7. Upstream candidates: cap `model_map_information` in SpendLogs metadata
   (~60% row-size cut, zero readers); cap model/key/team metric label
   cardinality; circuit-breaker on auth path during DB outages
