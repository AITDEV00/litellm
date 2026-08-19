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
| `deploy/prod/litellm-proxy.yaml` | Inline `Secret` `litellm-master-key` with `stringData.master-key` — **the single source of truth** |
| `deploy/prod/litellm-proxy.yaml` | Proxy container `LITELLM_MASTER_KEY` env (`secretKeyRef`) |
| `deploy/prod/discovery-controller.yaml` | Controller container `LITELLM_ADMIN_KEY` env (`secretKeyRef`) |
| `controller/config.py` | Local fallback: reads the manifest when `LITELLM_ADMIN_KEY` is unset (env still wins in-cluster) |
| `controller/README.md` | Documents the env var table |
| `config/local_dev.yaml` | `master_key: os.environ/LITELLM_MASTER_KEY` — reads from env (set by `make`) |
| `config/local_test_voice.yaml` | `master_key: os.environ/LITELLM_MASTER_KEY` — reads from env (set by `make`) |

### Everything derives from the one Secret

Rotating the value in `deploy/prod/litellm-proxy.yaml` and restarting both
Deployments covers all consumers:

- **Proxy** reads `LITELLM_MASTER_KEY` via `secretKeyRef` → `litellm-master-key`.
- **Discovery controller** reads `LITELLM_ADMIN_KEY` via `secretKeyRef` → the same Secret.
- **Docs** use `{{ master_key }}`, injected at build time by the MkDocs hook
  (`scripts/mkdocs_master_key.py`) straight from the manifest — no second copy.
- **Local tooling** (`make`, `config/*.yaml`, `.env.datasource`, benchmarks)
  derive the key from the manifest via `scripts/get_master_key.py` or the
  `os.environ/` mechanism.

So you change exactly **one** file (`deploy/prod/litellm-proxy.yaml`) and everything
else follows.

## Admin UI login

The LiteLLM Admin UI login is governed by `UI_USERNAME` / `UI_PASSWORD` env vars:

| Setting | Default | Notes |
|---------|---------|-------|
| `UI_USERNAME` | `admin` | Set via env if you want a non-default username |
| `UI_PASSWORD` | unset | **Falls back to the master key** if unset |

Logic lives in `litellm/proxy/auth/login_utils.py` → `get_ui_credentials()`.

- If `UI_PASSWORD` is unset, the UI password = the master key value.
- To decouple the UI password from the API master key, set `UI_PASSWORD`
  explicitly in `deploy/prod/litellm-proxy.yaml`.

## The Admin UI password vs. the API master key

| Concern | Env var | Where |
|---------|---------|-------|
| UI login password | `UI_PASSWORD` (falls back to master key) | `deploy/prod/litellm-proxy.yaml` container env |
| API `Authorization: Bearer` key | `LITELLM_MASTER_KEY` / `LITELLM_ADMIN_KEY` | Secret `litellm-master-key` |

If you want them the same value, set `UI_PASSWORD` to the same value as the
secret. If you want them independent, set `UI_PASSWORD` to something else.

## Rotation runbook (do this, not just change one file)

1. Edit the value in **`deploy/prod/litellm-proxy.yaml`** (the inline `Secret`) —
   this is the single place to change.
2. `kubectl apply -f deploy/prod/litellm-proxy.yaml` — this also updates the Secret.
3. **Restart BOTH Deployments** so pods re-resolve the secret:
   ```bash
   kubectl -n mlops rollout restart deployment/litellm-proxy
   kubectl -n mlops rollout restart deployment/oicm-discovery-controller
   ```
4. Wait for both rollouts and verify health:
   ```bash
   kubectl -n mlops rollout status deployment/litellm-proxy
   kubectl -n mlops rollout status deployment/oicm-discovery-controller
   ```
5. Verify the controller can still talk to the proxy (check controller logs for
   successful `/model/*` calls).
6. Rebuild the docs (`make docs` / `mkdocs build`) so the injected `{{ master_key }}`
   value reflects the new secret. Local `make` targets and benchmarks pick up the
   new value automatically from `deploy/prod/litellm-proxy.yaml`.

No other file needs a manual edit: docs, local configs, the controller fallback,
and benchmarks all derive from `deploy/prod/litellm-proxy.yaml`.

## Other secrets

| Secret | File | Consumed by |
|--------|------|-------------|
| `litellm-db-credentials` (DATABASE_URL) | `deploy/prod/litellm-proxy.yaml` | Proxy → Postgres |
| `litellm-redis-password` | `deploy/prod/litellm-proxy.yaml` | Proxy → Redis |

## Local / non-cluster configs that also hold a master key

| File | Value | Notes |
|------|-------|-------|
| `config/local_dev.yaml` | `os.environ/LITELLM_MASTER_KEY` | reads from env; `make` sets it from the manifest |
| `config/local_test_voice.yaml` | `os.environ/LITELLM_MASTER_KEY` | reads from env; `make` sets it from the manifest |
| `config/local_datasource.yaml` | `os.environ/LITELLM_MASTER_KEY` | reads from env |

All of these derive from `deploy/prod/litellm-proxy.yaml` via `scripts/get_master_key.py`,
so rotating the cluster secret automatically updates local runs too.