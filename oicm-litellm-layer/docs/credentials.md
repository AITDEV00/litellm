# Credentials & Secrets

> **Read this before changing any password or API key.** The most dangerous
> change in this repo is a credential rotation, because the **same secret is
> consumed by multiple deployments**. Missing one consumer breaks model
> discovery or proxy auth silently.

## The master key — a shared secret

The proxy's admin key (the master key) is stored in **one Kubernetes Secret**
and is read by **two different Deployments**:

| Consumer | Deployment | Env var | Source |
|----------|------------|---------|--------|
| LiteLLM Proxy | `litellm-proxy` | `LITELLM_MASTER_KEY` | `secretKeyRef` → `litellm-master-key` / key `master-key` |
| Discovery Controller | `oicm-discovery-controller` | `LITELLM_ADMIN_KEY` | `secretKeyRef` → `litellm-master-key` / key `master-key` |

Both read the **same** `litellm-master-key` secret's `master-key` field, so
rotating the secret value affects both. You must restart **both** Deployments
after a rotation, because env vars are snapshotted at pod creation.

### Where the value is defined

| Location | Purpose |
|----------|---------|
| `deploy/litellm-proxy.yaml` | Inline `Secret` `litellm-master-key` with `stringData.master-key` |
| `deploy/litellm-proxy.yaml` | Proxy container `LITELLM_MASTER_KEY` env (`secretKeyRef`) |
| `deploy/discovery-controller.yaml` | Controller container `LITELLM_ADMIN_KEY` env (`secretKeyRef`) |
| `controller/config.py` | **Hardcoded fallback** `os.getenv("LITELLM_ADMIN_KEY", "sk-1234")` — a local dev default, **not** used in-cluster (env overrides it) |
| `controller/README.md` | Documents the env vars table (says default `sk-1234`) |
| `config/local_dev.yaml` | Local dev config `master_key: sk-1234` |
| `config/local_test_voice.yaml` | Local test voice config `master_key: sk-1234` |

!!! warning "The fallback is a trap"
    `controller/config.py` falls back to `sk-1234` when `LITELLM_ADMIN_KEY` is
    unset. In production the Deployment always sets it via `secretKeyRef`, so
    the fallback never runs there. But if you run the controller locally without
    the env var, it silently uses `sk-1234` — which will stop matching a rotated
    secret. Keep this default in sync with the secret whenever you rotate.

## Admin UI login

The LiteLLM Admin UI login is governed by `UI_USERNAME` / `UI_PASSWORD` env vars:

| Setting | Default | Notes |
|---------|---------|-------|
| `UI_USERNAME` | `admin` | Set via env if you want a non-default username |
| `UI_PASSWORD` | unset | **Falls back to the master key** if unset |

Logic lives in `litellm/proxy/auth/login_utils.py` → `get_ui_credentials()`.

- If `UI_PASSWORD` is unset, the UI password = the master key value.
- To decouple the UI password from the API master key, set `UI_PASSWORD`
  explicitly in `deploy/litellm-proxy.yaml`.

## The Admin UI password vs. the API master key

| Concern | Env var | Where |
|---------|---------|-------|
| UI login password | `UI_PASSWORD` (falls back to master key) | `deploy/litellm-proxy.yaml` container env |
| API `Authorization: Bearer` key | `LITELLM_MASTER_KEY` / `LITELLM_ADMIN_KEY` | Secret `litellm-master-key` |

If you want them the same value, set `UI_PASSWORD` to the same value as the
secret. If you want them independent, set `UI_PASSWORD` to something else.

## Rotation runbook (do this, not just change one file)

1. Edit the value in **`deploy/litellm-proxy.yaml`** (the inline `Secret`).
2. Confirm the **controller fallback** in `controller/config.py` and the
   `controller/README.md` env table match (or are clearly documented as local-only).
3. `kubectl apply -f deploy/litellm-proxy.yaml` — this also updates the Secret.
4. **Restart BOTH Deployments** so pods re-resolve the secret:
   ```bash
   kubectl -n mlops rollout restart deployment/litellm-proxy
   kubectl -n mlops rollout restart deployment/oicm-discovery-controller
   ```
5. Wait for both rollouts and verify health:
   ```bash
   kubectl -n mlops rollout status deployment/litellm-proxy
   kubectl -n mlops rollout status deployment/oicm-discovery-controller
   ```
6. Verify the controller can still talk to the proxy (check controller logs for
   successful `/model/*` calls).

## Other secrets

| Secret | File | Consumed by |
|--------|------|-------------|
| `litellm-db-credentials` (DATABASE_URL) | `deploy/litellm-proxy.yaml` | Proxy → Postgres |
| `litellm-redis-password` | `deploy/litellm-proxy.yaml` | Proxy → Redis |

## Local / non-cluster configs that also hold a master key

| File | Value | Notes |
|------|-------|-------|
| `config/local_dev.yaml` | `sk-1234` | local dev proxy run |
| `config/local_test_voice.yaml` | `sk-1234` | local voice test |
| `config/local_datasource.yaml` | `os.environ/LITELLM_MASTER_KEY` | reads from env |

If you rotate the cluster secret, these local files are **not** affected unless
you also run the proxy locally and expect the same key to work.