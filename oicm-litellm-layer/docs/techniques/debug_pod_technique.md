# Debug Pod Technique — Copy-of-Replica for Isolated Profiling

> **Problem**: When a live endpoint is slow or intermittently fails (e.g. the
> model performance dashboard 30d / MTD / YTD views), you cannot profile the
> production pod in place. The production `litellm-proxy` runs `granian
> --num_workers 4`, so `kubectl exec` lands on an arbitrary worker, you can't
> attach a profiler to a specific PID, and you can't restart one worker to pick
> up an instrumented code path. `kubectl logs` is mostly router noise. And any
> change you make to the live Deployment risks an outage.

> **Solution**: Create a **debug Deployment that is a byte-for-byte copy of a
> production replica** (same image, env, secrets, config, volumes, node/tolerations)
> but with (a) a different label so it is **outside the production Service
> selector** (never receives live traffic) and (b) `--num_workers 1` so there is
> exactly ONE stable PID to exec/attach/probe/restart. You can then reproduce
> the slow path in isolation, instrument it, profile it, and delete it when done
> — all without touching the live replicas.

---

## Why The Production Pod Can't Be Used Directly

| Production reality | Debug-pod workaround |
|---|---|
| Granian forks 4 workers; `exec` hits an arbitrary one | `--num_workers 1` → one stable PID |
| Can't attach cProfile / pdb / py-spy to a specific worker | Attach to PID 1's worker reliably |
| Restarting the Deployment risks an outage (0 maxUnavailable) | Recreate the debug Deployment freely |
| Ingress timeout masks whether it's browser vs server | Hit the debug pod directly via port-forward |
| Live logs are drowned by router/upsert noise | Instrument + log only the path under test |

---

## The Technique

### 1. Reproduce the problem from OUTSIDE first (cheap, no pod)

Before spinning up any pod, measure the phases yourself from a local shell so
you know where time actually goes:

```bash
# 1a. DB query latency directly via psycopg (bypasses the app entirely)
.venv/bin/python3 /tmp/diag_rollup_latency.py
# -> "30d global: 96749 rows in 2.71s"   (the SQL is FAST)

# 1b. Python-side aggregation, isolated (bypasses HTTP + Prisma)
DATABASE_URL="<dsn>" timeout 180 .venv/bin/python3 -c "...call _rollup_minutes_to_model on real rows..."
# -> "python processing total: 0.87s"  (the Python is FAST)

# 1c. Full HTTP round-trip through the deployed gateway
curl -s -w "HTTP %{http_code} in %{time_total}s\n" \
  -H "Authorization: Bearer sk-1234" \
  "http://127.0.0.1:14000/model/performance?window=24h&start_time=...&end_time=..."
# -> "HTTP 200 in 20.531089s"  (SLOW, even though SQL + Python ≈ 4s)
```

If SQL is fast, Python is fast, but the HTTP round-trip is slow, the bottleneck
is the **Prisma client** (the gateway queries DB via `query_raw`, which streams
every row through Prisma's Rust engine as JSON over HTTP). That's your smoking
gun — a local psycopg connection doesn't reproduce it.

### 2. Create the debug Deployment

Copy the production `deploy/litellm-proxy.yaml` Deployment + Service into a new
file (`deploy/litellm-proxy-debug.yaml`) and change exactly these things:

| Field | Production | Debug |
|---|---|---|
| `metadata.name` | `litellm-proxy` | `litellm-proxy-debug` |
| label `app` | `litellm-proxy` | `litellm-proxy-debug` |
| Service `selector` / name | `litellm-proxy` | `litellm-proxy-debug` |
| `--num_workers` | `4` | `1` |
| `replicas` | `2` | `1` |
| `strategy` | RollingUpdate | Recreate (simpler for a scratch pod) |

Keep EVERYTHING else identical: image tag, env (secrets `litellm-master-key`,
`litellm-db-credentials`, `litellm-redis-password`), config/hook/logo ConfigMaps,
nodeSelector `adeo-gpu-03`, tolerations, resources, probes. Identical environment
guarantees the debug pod reproduces the production code path.

The full working manifest is at `deploy/litellm-proxy-debug.yaml`.

### 3. Deploy + verify it's isolated

```bash
export KUBECONFIG=/home/jyao/.kube/oicm-alain.conf
kubectl -n mlops apply -f deploy/litellm-proxy-debug.yaml
kubectl -n mlops rollout status deploy/litellm-proxy-debug

# It must NOT appear in the production Service's endpoints:
kubectl -n mlops get endpoints litellm-proxy -o jsonpath='{.subsets[*].addresses[*].ip}'
# -> only the 2 prod replicas' IPs; NOT the debug pod's IP
```

### 4. Instrument + probe from inside the pod

Because there's a single worker, you can attach a profiler to it:

```bash
POD=$(kubectl -n mlops get pod -l app=litellm-proxy-debug -o jsonpath='{.items[0].metadata.name}')

# cProfile the exact DB call the endpoint makes (in-process, no HTTP)
kubectl -n mlops exec "$POD" -- python3 -c "
import cProfile, pstats, io, os, asyncio
import litellm.proxy.model_metrics_endpoints.model_performance_endpoints as mpe
from datetime import datetime, timezone

async def main():
    start = datetime(2026, 7, 13, tzinfo=timezone.utc)
    end   = datetime(2026, 8, 12, tzinfo=timezone.utc)
    # The heavy client is created lazily; this isolates the DB read via Prisma.
    pr = await mpe._get_heavy_query_prisma_client()
    sql = '''SELECT model_group,bucket_start,request_count,completion_tokens,
        throughput_tokens_sum,ttft_seconds_sum,ttft_seconds_sum_sq,
        ttft_seconds_min,ttft_seconds_max,ttft_histogram_counts,starts,ends
        FROM \"LiteLLM_ModelPerformanceRollup\"
        WHERE bucket_start>=%s::timestamptz AND bucket_start<%s::timestamptz'''
    t0=time.monotonic()
    rows = await pr.db.query_raw(sql, start, end)
    print('query_raw rows=', len(rows), 'elapsed=', round(time.monotonic()-t0,2))
    await pr.disconnect()

asyncio.run(main())
"
```

This tells you definitively whether `Prisma.query_raw` (not psycopg) is the
slow path.

### 5. Hit the debug pod over HTTP in isolation

```bash
kubectl -n mlops port-forward deploy/litellm-proxy-debug 14001:4000 &
curl -s -w "HTTP %{http_code} in %{time_total}s\n" \
  -H "Authorization: Bearer sk-1234" \
  "http://127.0.0.1:14001/model/performance?window=24h&start_time=...&end_time=..."
```

Because it's a single worker, the request goes to a stable process you can
simultaneously `cProfile` or attach to, and you can read `kubectl logs` for that
one request with no interleaved router noise.

### 6. Clean up

```bash
kubectl -n mlops delete deploy/litellm-proxy-debug svc/litellm-proxy-debug
kubectl -n mlops scale deploy/litellm-proxy-debug --replicas=0   # or just delete
```

The debug Deployment holds GPU-node CPU/memory while running, so scale it to 0
or delete it when you're done.

---

## Cheat Sheet

| Action | Command |
|---|---|
| Apply debug pod | `kubectl -n mlops apply -f deploy/litellm-proxy-debug.yaml` |
| Exec a probe | `kubectl -n mlops exec deploy/litellm-proxy-debug -- python3 /app/probe.py` |
| Port-forward it | `kubectl -n mlops port-forward deploy/litellm-proxy-debug 14001:4000` |
| Read its logs | `kubectl -n mlops logs -l app=litellm-proxy-debug --tail=200` |
| Confirm isolation | `kubectl -n mlops get endpoints litellm-proxy` (debug pod absent) |
| Tear down | `kubectl -n mlops delete deploy/litellm-proxy-debug svc/litellm-proxy-debug` |

---

## Related

- [Logic Mapping Technique](logic_mapping_technique.md) — trace the full call
  chain before profiling, so the probe targets the right function.
- [Model Performance Optimization LOGIC MAP](../performance/model-performance-optimization-LOGIC-MAP.md)
  — the read path being debugged.