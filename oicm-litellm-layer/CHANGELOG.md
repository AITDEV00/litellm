# OICM-LiteLLM Integration Layer — Change Log

All notable changes to the OICM-LiteLLM integration layer are recorded here in
reverse chronological order (newest first).

---

## 2026-06-25

### Deployment YAML rewrite (`deploy/litellm-proxy.yaml`)

The previous `litellm-proxy.yaml` did not align with the actual LiteLLM source
code in the CWD. A full audit against the LiteLLM Dockerfile, `docker-compose.yml`,
`schema.prisma`, `litellm-proxy-extras`, and the running Docker container
(`docker.litellm.ai/berriai/litellm:main-stable`, v1.89.3) revealed five issues,
all now corrected.

#### Database: new `litellm` database on existing CloudNativePG cluster

LiteLLM requires its own PostgreSQL database for Prisma-managed tables
(`LiteLLM_ProxyModelTable`, `LiteLLM_VerificationToken`, `LiteLLM_SpendLogs`,
etc.). The existing OICM Postgres cluster (`mlops-postgres-1/2/3`, a 3-replica
CloudNativePG set in the `mlops` namespace) previously had only `oicm` and
`postgres` databases. LiteLLM must not share the `oicm` database because its
Prisma migrations manage their own schema.

Actions taken on the live cluster:

```
CREATE DATABASE litellm;
CREATE USER litellm WITH PASSWORD 'litellm_proxy_2025';
GRANT ALL PRIVILEGES ON DATABASE litellm TO litellm;
GRANT ALL ON SCHEMA public TO litellm;  -- inside the litellm database
```

Verified TCP connectivity from inside the pod:

```
kubectl exec -n mlops mlops-postgres-1 -- \
  env PGPASSWORD='litellm_proxy_2025' \
  psql -h mlops-postgres-rw.mlops -U litellm -d litellm \
  -c "SELECT current_user, current_database();"

#  current_user | current_database
# --------------+------------------
#  litellm      | litellm
```

The `DATABASE_URL` is now:
`postgresql://litellm:litellm_proxy_2025@mlops-postgres-rw.mlops:5432/litellm`

Stored in K8s Secret `litellm-db-credentials` (namespace `mlops`).

#### Image: corrected from `ghcr.io` to `docker.litellm.ai`

The previous YAML used `ghcr.io/berriai/litellm:main-stable`, which does not
exist on GHCR (the tag returns 404). The correct published image is
`docker.litellm.ai/berriai/litellm:main-stable`, confirmed by:

- `docker-compose.yml` line 7: `image: docker.litellm.ai/berriai/litellm:main-stable`
- Running container inspection: image created 2026-06-20, version 1.89.3
  (matches CWD `git describe --tags` = `v1.89.3`)
- Container entrypoint: `/app/docker/prod_entrypoint.sh` → `exec litellm "$@"`

The Makefile `LITELLM_IMG` variable was also updated from
`ghcr.io/berriai/litellm:main-stable` to
`docker.litellm.ai/berriai/litellm:main-stable` to match.

#### Migrations: removed initContainer, rely on built-in `prisma migrate deploy`

The previous YAML had an `initContainer` that ran `pip install` into
`site-packages`. This does not work with the published image because the image
uses a virtualenv at `/app/.venv` (not system `site-packages`), and `pip` is not
even on the `PATH` (confirmed via `docker exec`).

The published image runs migrations automatically on startup. The startup
sequence is:

1. `prod_entrypoint.sh` calls `exec litellm "$@"`.
2. `litellm` CLI (`proxy_cli.py`) calls `PrismaManager.setup_database()`.
3. `PrismaManager.setup_database()` delegates to
   `litellm_proxy_extras.ProxyExtrasDBManager.setup_database()`.
4. `ProxyExtrasDBManager` runs `prisma migrate deploy` (127 migrations in
   `prisma/migrations/`).

Confirmed from container logs:
```
litellm_proxy_extras - INFO - Running prisma migrate deploy
prisma migrate deploy stdout: Prisma schema loaded from schema.prisma
Datasource "client": PostgreSQL database "litellm" at "db:5432"
127 migrations found in prisma/migrations
Applying migration `20250326162113_baseline`
...
```

The deployment command now includes `--use_v2_migration_resolver` (the safer
resolver that avoids schema thrashing during rolling deploys, as recommended in
`helm/litellm/values.yaml`).

#### Health endpoints: corrected from `/health` to `/health/readiness` and `/health/liveliness`

The previous YAML used `/health` for both probes. The correct endpoints are:
- Readiness: `/health/readiness` (from `docker-compose.yml` healthcheck and
  `helm/litellm/values.yaml`)
- Liveliness: `/health/liveliness` (from `docker-compose.yml` healthcheck and
  `helm/litellm/values.yaml`)

#### Hooks: corrected mounting path and callback string

The callback loading mechanism (`get_instance_fn` in
`litellm/proxy/types_utils/utils.py`) resolves module paths relative to the
config file directory. Since `config.yaml` is mounted at `/app/config.yaml`,
the callback string `litellm_hooks.vllm_param_injector.vllm_param_injector`
resolves to `/app/litellm_hooks/vllm_param_injector.py` and extracts the
`vllm_param_injector` module-level instance.

The ConfigMap `litellm-hooks` is mounted at `/app/litellm_hooks/` (read-only).
The callback string in the config is `litellm_hooks.vllm_param_injector.vllm_param_injector`.

#### Secrets: master key moved from ConfigMap to K8s Secret

The `LITELLM_MASTER_KEY` and `DATABASE_URL` are now in K8s Secrets
(`litellm-master-key` and `litellm-db-credentials`) instead of hardcoded in the
ConfigMap. The discovery controller's `LITELLM_ADMIN_KEY` env var was updated to
reference `litellm-master-key` / `master-key` (was previously
`litellm-admin-key` / `key`).

### Discovery controller secret reference fix (`deploy/discovery-controller.yaml`)

Updated the `LITELLM_ADMIN_KEY` secret reference from `litellm-admin-key`/`key`
to `litellm-master-key`/`master-key` so both manifests reference the same secret.

### Makefile image fix (`Makefile`)

Changed `LITELLM_IMG` from `ghcr.io/berriai/litellm:main-stable` to
`docker.litellm.ai/berriai/litellm:main-stable` to match the actual published
image and the deployment YAML.

---

## 2026-06-24

### Component #2 (Custom Auth) removed

The custom auth handler (`auth/oicm_auth.py`) was removed. LiteLLM's native
virtual key management handles per-user, per-model access control. Since LiteLLM
runs in the `mlops` namespace and the model services are ClusterIP services in
the `adeo` namespace, LiteLLM can reach them directly without OICM DB
authentication. The `auth/` directory was deleted, and all references to
`custom_auth` and `OICM_DB_URL` were removed from the config and deployment
manifests.

### Discovery controller rewrite and test (`controller/discovery.py`)

The discovery controller was rewritten with proper K8s config loading
(`load_incluster_config()` for in-cluster, `load_kubeconfig()` for dev), a
non-blocking watch loop (moved to thread executor), and fixed imports. Tested
successfully against the live cluster, registering 20 models in LiteLLM.

### Makefile created (`Makefile`)

Created build/push/deploy automation using podman. All podman commands use
`--tls-verify=false` for the internal Harbor registry. Targets: `pull`,
`build`, `login`, `push`, `deploy`, `clean`.

---

## 2026-06-23

### Project scaffolding

Initialized the `oicm-litellm-layer` project with `uv` and Python 3.12. Created
`pyproject.toml` with dependencies: `kubernetes>=28.0`, `httpx>=0.25`,
`aiohttp>=3.9`.

### Architecture validation

Validated all 7 LiteLLM extension points against the actual source code in the
CWD:
1. `/model/new` and `/model/delete` REST APIs for dynamic model registration
2. `CustomLogger.async_pre_call_hook` for request interception
3. `CustomLogger.async_log_*` methods for Prometheus metrics
4. `config.yaml` with `litellm_settings.callbacks` for plugin loading
5. `get_instance_fn` for module path resolution relative to config file
6. `store_model_in_db: true` for DB-backed model persistence
7. `schema.prisma` with 127 Prisma migrations for database schema
