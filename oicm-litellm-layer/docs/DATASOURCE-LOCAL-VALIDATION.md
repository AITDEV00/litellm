# DATASOURCE-LOCAL-VALIDATION.md
#
# Run a LOCAL LiteLLM proxy against the deployed cluster's metrics
# datasources (Postgres / Redis / Prometheus) without forwarding any LLM model
# service. Use this to validate backend metric/UI/spend changes quickly against
# real data.
#
# End-to-end flow:
#   1. Open the SSH tunnel to the Al Ain API server
#   2. kubectl port-forward the three datasources (script provided)
#   3. Run litellm locally with env vars pointing at the forwarded ports
#   4. Validate /health, /v1/models, /model/performance, UI
#
# No LLM service is port-forwarded: model_list stays empty and models come from
# Postgres (store_model_in_db), so live generation calls will fail. The goal is
# validating the METRICS / ADMIN / SPEND / UI surfaces against real data.

---

## 1. Context

The deployed proxy (`mlops` namespace) reads three datasources:

| Datasource | Deployed URL / secret                                   | Kind   | Namespace             |
|------------|----------------------------------------------------------|--------|-----------------------|
| Postgres   | `postgresql://litellm:...@mlops-postgres-rw.mlops:5432/litellm` | svc | `mlops` |
| Redis      | `litellm-redis.redis.svc.cluster.local:6379` (auth)      | svc    | `redis` |
| Prometheus | `http://kube-prometheus-stack-prometheus.kube-prometheus-stack:9090` | svc | `kube-prometheus-stack` |

Local mapping used by the forward script:

| Local port | Remote target                  | Purpose                                   |
|------------|--------------------------------|-------------------------------------------|
| `5432`     | `svc/mlops-postgres-rw:5432`   | Prisma DB (models, keys, spend, logs)     |
| `16379`    | `svc/litellm-redis:6379`       | Redis cache + auth cache                  |
| `9090`     | `svc/kube-prometheus-stack-prometheus:9090` | /model/performance, per-model metrics |

> The Al Ain API server `https://api.adeoaiengine.ecouncil.ae:6443` is only
> reachable from the bastion, so the SSH tunnel below is the first step.

---

## STEP 1 - Open the SSH tunnel

```bash
sshpass -p '<BASTION_PASSWORD>' ssh -fN \
  -o StrictHostKeyChecking=no \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o ExitOnForwardFailure=yes \
  -L 6443:10.34.104.10:6443 \
  adeo@10.34.104.99
```

This binds `127.0.0.1:6443` and forwards it to `10.34.104.10:6443` (the Al Ain
API server). Keep it running for the whole session. Verify:

```bash
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}'
# https://api.adeoaiengine.ecouncil.ae:6443
kubectl cluster-info   # reachable via tunnel
```

---

## STEP 2 — Port-forward the datasources

Use the provided script (it starts the three forwards in the background, prints
a reachability check, and traps cleanup on Ctrl+C):

```bash
cd oicm-litellm-layer/scripts
./port-forward-datasources.sh --validate
```

Expected output:

```
Port-forwards active (Ctrl+C to stop):
  Postgres:   127.0.0.1:5432   (mlops/mlops-postgres-rw)
  Redis:      127.0.0.1:16379  (redis/litellm-redis)
  Prometheus: 127.0.0.1:9090   (kube-prometheus-stack/prometheus)
cluster-info: API reachable via tunnel
```

If you prefer raw commands (no script):

```bash
kubectl -n mlops port-forward svc/mlops-postgres-rw 5432:5432 &
kubectl -n redis port-forward svc/litellm-redis 16379:6379 &
kubectl -n kube-prometheus-stack port-forward svc/kube-prometheus-stack-prometheus 9090:9090 &
```

Verify each is bound:

```bash
ss -tlnp | grep -E ':5432|:16379|:9090'
```

---

## STEP 3 — Create the local env file

Copy the template and fill in the secrets (get the real values from the
deployed config map / secrets):

```bash
cp oicm-litellm-layer/.env.datasource.example oicm-litellm-layer/.env.datasource
```

```
DATABASE_URL=postgresql://litellm:litellm_proxy_2025@127.0.0.1:5432/litellm
REDIS_HOST=127.0.0.1
REDIS_PORT=16379
REDIS_PASSWORD=litellm-redis-<...>
PROMETHEUS_URL=http://127.0.0.1:9090
LITELLM_MASTER_KEY={{ master_key }}
STORE_MODEL_IN_DB=true
```

---

## STEP 4 — Run litellm locally

> IMPORTANT: put the venv `bin` dir on PATH first. litellm locates the `prisma`
> CLI via `subprocess.run(["prisma", ...])`; if it is not on PATH it prints
> "prisma package not found" and runs with no DB. Activating the venv (or adding
> `.venv/bin` to PATH) fixes this.

```bash
cd <litellm-repo-root>
set -a; source oicm-litellm-layer/.env.datasource; set +a
export PATH="$PWD/.venv/bin:$PATH"
.venv/bin/litellm \
  --config oicm-litellm-layer/config/local_datasource.yaml \
  --port 4000 \
  --detailed_debug
```

On startup litellm should:

1. connect Prisma to the forwarded Postgres (spend/keys/logs loaded)
2. connect to the forwarded Redis for caching
3. expose the Prometheus callback so `/model/performance` reads the forwarded Prometheus

Watch the startup log for `prisma migrate deploy ... No pending migrations`
then `Uvicorn running on http://0.0.0.0:4000`.

---

## STEP 5 — Validate

Health + model list (master key `{{ master_key }}`):

```bash
curl -s http://localhost:4000/health/liveliness
curl -s http://localhost:4000/health/readiness
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer {{ master_key }}"
```

Model performance (reads Prometheus; earlier in `_fetch_prometheus_performance`):

```bash
curl -s "http://localhost:4000/model/performance?window=1h" \
  -H "Authorization: Bearer {{ master_key }}"
```

Expected a `source: "prometheus"` set of per-model series (tokens, latency,
concurrency, ttft). To exercise the DB path, scope the query to an entity/key:

```bash
curl -s "http://localhost:4000/model/performance?window=1d&api_key=..." \
  -H "Authorization: Bearer {{ master_key }}"
```

Admin API / spend surfaces (reads Postgres):

```bash
curl -s http://localhost:4000/spend/logs -H "Authorization: Bearer {{ master_key }}" | head
curl -s http://localhost:4000/model/metrics  -H "Authorization: Bearer {{ master_key }}"
```

UI (bundled in the proxy):

```text
http://localhost:4000/ui/?page=model-performance
http://localhost:4000/ui/?page=logs
```

---

## Verified (2026-08-05)

The full flow was smoke-tested against the live cluster:

- `prisma migrate deploy` connected to the forwarded Postgres:
  `Datasource "client": PostgreSQL database "litellm", schema "public" at "127.0.0.1:5432" ... No pending migrations`
- `GET /v1/models` returned the real deployed models from Postgres
  (`vibevoice`, `Qwen/Qwen3-ASR-1.7B`, `Qwen/Qwen3-Embedding-4B`, ...)
- `GET /model/performance?window=1h` returned
  `{"window":"1h","source":"prometheus","step":"30s","models":[...]}` from the
  forwarded Prometheus
- `/health/liveliness` and `/health/readiness` returned `200`

---

## Notes / limitations

- Model generation will not work: the config has an empty `model_list` and no
  LLM service is port-forwarded. This validates the metric/admin/spend/UI
  surfaces, not live routing.
- Values above are from the current `mlops` deployment; refresh them from the
  live pod if they change (see `kubectl -n mlops get deploy litellm-proxy -o yaml`).
- The `PROMETHEUS_URL` is read at import time in `prometheus_api.py`, so the env
  var must be set before the proxy starts.
- `/model/performance` needs the `prometheus` callback in `litellm_settings.callbacks`
  (present in `local_datasource.yaml`) for the Prometheus path to be used.
- `pkill -f kubectl port-forward` will clear stray forwards.