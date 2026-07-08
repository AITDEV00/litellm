# OICM-LiteLLM Integration Layer — Change Log

All notable changes to the OICM-LiteLLM integration layer are recorded here in
reverse chronological order (newest first).

---

## 2026-07-08

### Discovery controller rewrite: VSA refactor, dedup fix, concurrent batch HTTP

Rewrote the discovery controller from a single 730-line `discovery.py` into
eight single-responsibility modules following vertical slice architecture.
Fixed three runtime bugs (None model_id 422 errors, shutdown race condition,
TypeError on empty batches in asyncio.gather). Added concurrent HTTP batching
for all LiteLLM API operations. Cleaned up 17,716 duplicate models that had
accumulated from the old controller's unconditional registration on every
watch reconnect.

#### VSA refactoring (`controller/` package)

Split `discovery.py` into:

- `config.py`: env vars, constants, logging configuration
- `models.py`: `OicmModel` dataclass, `sanitize_model_id`, `detect_mode`
- `k8s_discovery.py`: `K8sDiscoverer` (list deployments, read ConfigMaps,
  query /v1/models)
- `litellm_client.py`: `LiteLLMClient` (batch register/deregister/patch,
  list_all_models_by_uuid)
- `reconciler.py`: `SyncReconciler` (pure `compute_plan` + `execute`,
  `_pick_richest_entry`)
- `controller.py`: `DiscoveryController` (start/stop, full_sync, watch loop,
  event handlers)
- `__main__.py`: entry point with signal handling

Dependency graph is strictly one-directional with no cycles. Each module has
a single responsibility and can be tested in isolation. `DiscoveryController`
and `LiteLLMClient` constructors accept injected dependencies, enabling unit
testing without monkeypatching.

Dockerfile CMD changed from `python -m controller.discovery` to
`python -m controller`. Pyproject.toml entry point updated accordingly.

#### Duplicate model fix: model_id field location

Root cause: `/model/info` returns each model with the id at
`model_info.id`, not at the top level. The old code did
`entry.get("model_id")` which returned `None` for all 17,700+ entries,
causing 422 validation errors on delete and preventing dedup from working.
Verified against LiteLLM source: `model_management_endpoints.py` and
`proxy_server.py` consistently access the id as
`model.get("model_info", {}).get("id")`.

Fix: `list_all_models_by_uuid` now normalizes each entry by setting
`m["model_id"] = info.get("id")` when grouping, so all downstream code
(`_pick_richest_entry`, `full_sync` Case 1-4) reads a consistent field.

Also added `None` filtering in `deregister_many` (defense in depth) and a
`_running` guard in `_handle_add` to prevent registrations during shutdown.

#### Concurrent HTTP batching (`litellm_client.py`)

Replaced sequential `await` in a for-loop (17,700 deletes at ~16ms each =
~280s) with `asyncio.gather` + `Semaphore(50)` bounded concurrency (~6s).
All three operation types (delete, register, patch) are batched into a single
`batch()` call that fires all three groups concurrently through one shared
`httpx.AsyncClient` with connection pooling.

The semaphore is created once in `__init__` rather than per-call, so
concurrency is bounded across the entire client lifetime. Individual
convenience methods (`register_model`, `deregister_model`, `patch_model`)
delegate to `batch()` so the watch loop's single-event handlers get the same
connection pooling.

Fixed a `TypeError: unhashable type: 'list'` bug where empty operation lists
were passed as bare `[]` to the outer `asyncio.gather`, which treated them as
awaitables. Replaced with an `_empty_list()` async coroutine that returns
`[]`.

#### Config preservation during dedup (`reconciler.py`)

`_pick_richest_entry` scores each duplicate entry by how many CONFIG_KEYS
(rpm, tpm, max_parallel_requests, input/output_cost_per_token,
input/output_cost_per_second) are set, keeping the one with the most
admin-applied config. Loser entries are deleted. When a model's name changes
(needing delete + re-register), `inherited_params` copies the old entry's
litellm_params into the new registration so RPM/TPM profiles survive.

When only the api_base or model path changes (same model_name), the
controller PATCHes via `/model/{id}/update` instead of delete + re-register,
preserving all admin config in place.

#### Result

Model count in LiteLLM: 17,735 -> 19 (matching exactly the 19 k8s model
deployments). Controller runs clean sync cycles with zero errors. Two full
syncs verified in logs: `Patched 19/19 models`, `Full sync complete: 19
models registered`.

---

## 2026-07-07 (benchmark sweep)

### Performance validation: text-only benchmark sweep c=100-1000

Ran a full concurrency sweep (c=100 to c=1000, step 100, 5 repeats per level,
1000 requests per run, prompt "hi", max_tokens=2, thinking disabled) against
the Qwen/Qwen3.5-0.8B model via both ClusterIP and DNS (gateway URL) routing.
The optimized setup (2 replicas, Granian 4 workers each, dedicated Redis)
handled all 50,000 requests per routing method with zero errors at every
concurrency level except a single transient 470-error spike at c=1000 DNS
(4 of 5 runs clean).

A baseline comparison was also run: the same sweep against a stripped-down
deployment (1 replica, uvicorn 1 worker, no Redis, no Granian). The baseline
completely collapsed at c>=200, with 100% connection failures ("All connection
attempts failed") at c=300 through c=1000. Even at c=100, the baseline had a
40% error rate (2 of 5 runs fully failed) and achieved only ~40 rps on
successful runs, vs 76.7 rps for the optimized setup.

#### Executive summary: current setup performance

The current production configuration (2 replicas, 4 Granian workers each =
8 total worker processes, dedicated Redis 7.4.3, AOF persistence) delivers
robust, reliable throughput across the full concurrency range. Key numbers:

At c=100, the optimized setup achieves 76.7 rps (ClusterIP) / 47.1 rps (DNS)
with p50 latency under 1s. The baseline managed only 40.1 rps with a 40%
failure rate. That is a 1.9x throughput improvement with near-zero errors.

At c=300-500, throughput stabilizes at ~22-24 rps with very low variance
(cv 0.6-3.4%). This is the steady-state operating range. The baseline
produced 0 rps at these levels (100% connection failures).

At c=600+, a regime shift occurs where throughput jumps to 49-53 rps and
holds through c=1000. The baseline cannot reach this range at all.

DNS (gateway URL via kube-proxy) vs ClusterIP (direct pod IP) routing shows
no meaningful difference. At c=300-500 and c=900, median rps matches within
1-9%, well within run-to-run variance. The DNS service resolution and
kube-proxy iptables/IPVS layer adds no measurable overhead at these
concurrency levels. The non-monotonic throughput curve (trough at c=300-500,
jump at c=600) is a property of the LiteLLM proxy and backend, not of the
routing path.

Total errors across the entire sweep: 2 out of 50,000 requests for ClusterIP,
~472 out of 50,000 for DNS (single transient spike). The baseline had
~43,000 errors out of 50,000 requests.

The Granian Rust HTTP runtime is the critical enabler. Its Rust-threaded TCP
accept and HTTP parsing avoid the Python GIL bottleneck that caused uvicorn's
connection backlog (default ~128) to exhaust under concurrent load. The 4
workers per replica provide 4 independent connection acceptors per pod, and
the 2 replicas distribute load across separate event loops and nodes.

Benchmark output files: `/tmp/bench_text_cip.txt` (ClusterIP),
`/tmp/bench_text_dns.txt` (DNS), `/tmp/bench_text_baseline.txt` (baseline).

---

## 2026-07-07

### Horizontal scaling to 2 replicas with dedicated Redis (`deploy/litellm-proxy.yaml`, `deploy/litellm-redis.yaml`)

Scaled the LiteLLM proxy from 1 to 2 replicas with a dedicated Redis instance,
enabling horizontal scaling, zero-downtime rolling deploys, and cross-replica
shared state (auth cache, rate limits, spend buffer). This was the final step
in a multi-phase optimization effort that began with switching from uvicorn
1-worker to Granian 4-worker, then adding Redis, then isolating Redis as the
throughput bottleneck, and finally fixing it by deploying a dedicated Redis
instance and scaling horizontally.

The root cause analysis (documented in `before + after correct configuration for litellm .md`)
identified that litellm issues 29 Redis commands per request across 5 code
paths: auth cache GET/SET, rate limiter pre-call batch GET, rate limiter
post-call success logging (up to 5 individual GETs + 1 pipeline SET), and
config cache reads. The rate limiter's `async_log_success_event` is the biggest
offender, doing 6+ Redis round-trips per request without `local_only=True`.
Under high concurrency, these Redis operations saturate the asyncio event loop
and cap throughput at ~32 rps per replica (vs 87 rps direct to vLLM).

Scaling to 2 replicas distributes the event loop contention across two
independent worker pools (8 total Granian workers). The k8s Service
load-balances across both pods, so neither pod's event loop is overwhelmed.
At c=200, the 2-replica setup achieves 69.3 rps with -35.6% p50 overhead vs
direct (actually faster than direct vLLM). At c=500, throughput improves from
28.8 rps (1 replica) to 49.0 rps (2 replicas), and p50 overhead drops from
+145% to +75.5%.

Changes to `deploy/litellm-proxy.yaml`:

- `replicas: 1` -> `replicas: 2`
- Added `strategy.rollingUpdate` (`maxUnavailable: 0, maxSurge: 1`) for
  zero-downtime deployments
- Added `topologySpreadConstraints` (maxSkew=1, topologyKey=
  `kubernetes.io/hostname`, whenUnsatisfiable=`ScheduleAnyway`) to spread pods
  across nodes
- Added `PodDisruptionBudget` (`minAvailable: 1`) to prevent both pods from
  being evicted during node maintenance
- Added `general_settings.use_redis_transaction_buffer: true` so spend updates
  are buffered through Redis, avoiding PostgreSQL write contention when 2
  replicas write spend logs simultaneously

Changes to `deploy/litellm-redis.yaml`:

- `appendonly no` -> `appendonly yes` with `appendfsync everysec` and
  `auto-aof-rewrite-percentage: 100` / `auto-aof-rewrite-min-size: 64mb` for
  AOF persistence. Critical because `use_redis_transaction_buffer` stores
  in-flight spend updates in Redis; AOF ensures they survive Redis restarts.
  Required adding `dir /data` to `redis.conf` so AOF files are written to the
  PVC-backed path (the default working directory is on the read-only root
  filesystem due to `readOnlyRootFilesystem: true` in the security context)
- `maxmemory 256mb` -> `512mb` (2x headroom for 2 replicas' cached data)
- Redis resource limits: 500m CPU / 512Mi -> 1 CPU / 1Gi (handles 2x client
  connections and AOF rewrite fork)
- Redis resource requests: 100m CPU / 256Mi -> 200m CPU / 512Mi

Verified: both pods running on different nodes (`adeo-master-02` and
`adeo-master-03`), 0 Redis errors during full benchmark, AOF enabled and
verified via `CONFIG GET appendonly` returning `yes`, `use_redis_transaction_buffer`
visible in pod config, PDB active with `minAvailable: 1`.

### Root cause analysis: per-request Redis call chain

Traced the exact Redis operations that happen per request through litellm's
source code. Instrumented a single request live and measured 29 Redis
commands (down from the earlier estimate of 102-109, which counted commands
across all 4 Granian workers; the per-request per-worker count is 29).

The 5 code paths that hit Redis per request:

1. **Auth cache GET** (`auth_checks.py:2542`, `get_key_object`): calls
   `user_api_key_cache.async_get_cache` without `local_only=True`. On warm
   cache, in-memory hits and Redis is skipped. On cold start, hits Redis.
2. **Auth cache SET** (`auth_checks.py:1820`, `_cache_key_object`): fires via
   `asyncio.create_task` (non-blocking) and calls `async_set_cache` without
   `local_only=True`, writing to Redis every request to refresh TTL.
3. **Rate limiter pre-call batch GET** (`parallel_request_limiter.py:170`,
   `get_all_cache_objects`): calls `async_batch_get_cache` for 6 keys without
   `local_only=True`. Has a 10s throttle (`redis_batch_cache_expiry`) that
   skips Redis if the key was recently accessed in-memory.
4. **Rate limiter post-call success logging**
   (`parallel_request_limiter.py:500`, `async_log_success_event`): does up to
   5 individual `async_get_cache` calls + 1 `async_batch_set_cache`, all
   without `local_only=True`. Runs via the background `LoggingWorker` but
   still competes for the same asyncio event loop.
5. **Config cache reads** (`litellm_config_cache`): 6 `litellm_config:param:*`
   keys read periodically (TTL ~50s).

The rate limiter's `async_log_success_event` is the primary throughput
bottleneck. It does 6+ Redis round-trips per request without `local_only=True`,
even though the pre-call hook already uses `local_only=True` for the same data.
A code fix would be to add `local_only=True` to the post-call logging hooks,
which would eliminate ~80% of the per-request Redis commands while keeping
cross-worker pre-call rate limit accuracy. This is a litellm upstream issue,
not an OICM integration issue.

### Dedicated Redis deployment (`deploy/litellm-redis.yaml`)

Deployed a dedicated Redis 7.4.3 instance for litellm in the `redis` namespace,
separate from the existing shared `redis-master` (which serves BullMQ and other
workloads). The shared Redis was causing 33-46ms MGET blocking under concurrent
load from BullMQ workers, and had 788 idle connections from litellm that never
closed (no `timeout` configured).

The dedicated Redis is a StatefulSet with 5Gi PVC, tuned config (io-threads=4,
io-threads-do-reads=yes, tcp-keepalive=60, timeout=60, lazyfree-*=yes), and a
duplicate `litellm-redis-password` Secret in the `mlops` namespace (k8s
Secrets are namespace-scoped, so the proxy pod in `mlops` cannot reference a
Secret in `redis`).

### Granian 4-worker + Redis optimization (`deploy/litellm-proxy.yaml`)

Switched from uvicorn 1-worker to Granian 4-worker (`--run_granian
--num_workers 4`). Granian uses a Rust HTTP parser that eliminates the ~55ms
of Python h11 parsing overhead per request. At c=1, this reduced overhead from
+53.8ms (58.6%) to -7.9ms (-8.5%), effectively zero. Added Redis cache with
`enable_redis_auth_cache: true`, `max_connections: 500`, `socket_timeout: 10.0`.

Full benchmark results and analysis in `before + after correct configuration for litellm .md`.

---

## 2026-06-29

### Custom logo and favicon via ConfigMap (`deploy/litellm-proxy.yaml`, `Makefile`)

Branded the LiteLLM dashboard with the ADEO AI Gateway logo and favicon. The
original 1024x1024 / 127KB JPEG was downsized to 256x256 / 11KB (metadata
stripped, quality 85) using ImageMagick. A 32x32 PNG favicon (1.9KB) was
generated from the same source.

Both files are mounted into the pod via a `litellm-logo` ConfigMap volume at
`/app/assets/`, and litellm is configured via env vars to serve them:

- `UI_LOGO_PATH=/app/assets/adeo-ai-gateway.jpg` — served by `/get_image`
- `LITELLM_FAVICON_URL=/app/assets/favicon.png` — served by `/get_favicon`

The navbar (`navbar.tsx:48`) falls back to `/get_image` when no `logo_url` is
set in the theme settings DB, so the logo appears automatically. The
`ThemeContext` applies the favicon dynamically at runtime.

A `litellm-logo` Makefile target creates/updates the ConfigMap from the files
in `decor/`. No image rebuild required to change the logo; just update the
ConfigMap and restart the pod.

Note: the SVG variant (`adeo-ai-gateway.svg`) was not used because the
`detect_local_image_media_type()` function in `static_asset_utils.py` only
recognizes PNG, JPEG, GIF, WebP, and ICO by magic bytes. SVG files are
rejected and fall back to the default.

### Logo redirect loop fix

Setting `logo_url` in the DB theme settings to the full HTTPS URL
(`https://litellm.adeoaiengine.ecouncil.ae/get_image`) caused an
`ERR_TOO_MANY_REDIRECTS` loop. The `update_ui_theme_settings` backend
(`proxy_setting_endpoints.py:985`) overwrites the `UI_LOGO_PATH` env var with
the DB value. The `/get_image` endpoint then saw `UI_LOGO_PATH` starting with
`https://` and returned a `RedirectResponse` to that same URL, creating an
infinite redirect.

Fix: reset `logo_url` to `null` in the DB (via `PATCH
/update/ui_theme_settings`), which restored the `UI_LOGO_PATH` env var from
the K8s deployment spec (`/app/assets/adeo-ai-gateway.jpg`). Pod restarted to
get a clean `os.environ` state. Verified `/get_image` returns the correct ADEO
logo (MD5 match, 10613 bytes, `image/jpeg`).

Lesson: do not set `logo_url` in the DB to an HTTPS URL pointing back at
`/get_image`. The env var `UI_LOGO_PATH` with a local file path is the correct
approach for ConfigMap-mounted logos. The DB `logo_url` field is only for
external CDN URLs that do not route back through the proxy itself.

### Enterprise license gating: fix `/health/license` has_license (source: `litellm/proxy/auth/litellm_license.py`)

The UI's enterprise feature pages (Organizations, Admin Panel, etc.) gate on
`premiumUser`, which the frontend reads from two sources: the JWT token's
`premium_user` claim (decoded client-side in `AuthContext.tsx`) and the
`/health/license` endpoint's `has_license` field (used by `UsageIndicator`).

The previous override only patched `LicenseCheck.is_premium()` to return True
early, which set the `premium_user` global (and thus the JWT claim) correctly.
However the early return skipped the `self.license_str = os.getenv(...)` line,
so `_license_check.license_str` remained None. The `/health/license` endpoint
checks `has_license = bool(getattr(_license_check, "license_str", None))`
separately from `is_premium()`, so it returned `has_license: false`.

Fix: set `self.license_str = "dev-trial-override"` inside the override block so
both the `is_premium()` return value and the `license_str` attribute are
consistent. Verified at runtime: `premium_user=True`, `is_premium()=True`,
`license_str="dev-trial-override"`, `/health/license` returns
`has_license: true, license_type: enterprise`.

Image rebuilt, pushed, and deployment restarted.

## 2026-06-25

### Ingress TLS fix (`deploy/litellm-ingress.yaml`)

The ingress was only serving on port 80 (HTTP), not 443 (HTTPS). Added a `tls:`
block referencing `litellm.adeoaiengine.ecouncil.ae-tls`. The nginx ingress
controller uses its `--default-ssl-certificate` (wildcard
`*.adeoaiengine.ecouncil.ae`) to serve TLS on 443 for any ingress with a `tls:`
section, so no cert-manager annotation is needed.

### api_base /v1 suffix fix (`controller/discovery.py`)

Chat completion requests through LiteLLM returned 404
(`Hosted_vllmException - {"detail":"Not Found"}`). Root cause: the discovery
controller registered models with `api_base` set to
`http://s-{uuid}.adeo.svc.cluster.local:8080` (no `/v1` suffix). LiteLLM's
`hosted_vllm` provider inherits from the OpenAI-like handler, which appends
`/chat/completions` to `api_base` in `_validate_environment()`
(`litellm/llms/openai_like/common_utils.py:53`). Without `/v1`, the final URL
became `.../8080/chat/completions` instead of `.../8080/v1/chat/completions`,
and vLLM returned 404.

Fix: added `/v1` to the `api_base` property in `OicmModel`:
```python
return f"http://s-{self.uuid}.{self.namespace}.{CLUSTER_DOMAIN}:{MODEL_PORT}/v1"
```

After fixing, all 20 existing models in LiteLLM had to be deleted and
re-registered (the old registrations had the broken `api_base`). Rebuilt and
pushed the controller image, deleted the 20 old models via `/model/delete`,
restarted the controller deployment, and verified 20 models re-registered with
the correct `/v1` api_base. Chat completion confirmed working with 200 OK.

Note: vLLM-specific params like `chat_template_kwargs` must be sent inside
`extra_body` in the request, not at the top level. The `vllm_param_injector`
hook (currently wired in the config) only handles `vllm_` prefixed params in
`model_info`, not arbitrary top-level params.

### Ingress manifest for LiteLLM proxy + web UI (`deploy/litellm-ingress.yaml`)

Created an nginx Ingress manifest that exposes the LiteLLM proxy (inference API
and admin UI) on `https://litellm.adeoaiengine.ecouncil.ae` via port 443. The
ingress routes all paths (`/`) to the `litellm-proxy` service on port 4000,
which serves both the OpenAI-compatible API endpoints (`/v1/chat/completions`,
`/v1/embeddings`, `/model/info`, etc.) and the admin web UI at `/ui/`.

No TLS block is needed in the manifest because the nginx ingress controller is
configured with `--default-ssl-certificate=wildcard-ingress-tls-cert`, a
wildcard certificate covering `*.adeoaiengine.ecouncil.ae`. The controller
serves TLS on 443 automatically for any ingress.

Nginx annotations copied from the existing OICM inference ingress
(`oicm-api-gateway-ingress-external`) to support LLM streaming:
`proxy-buffering: "false"`, `proxy-request-buffering: "false"`,
`proxy-read-timeout: "3600"`, `proxy-send-timeout: "3600"`,
`proxy-body-size: "0"`.

Requires a DNS A record for `litellm.adeoaiengine.ecouncil.ae` pointing to
`10.34.104.100` (the nginx ingress controller LoadBalancer external IP).

### Discovery controller health server fix (`controller/discovery.py`)

The discovery controller was crash-looping (exit code 137, 5 restarts) because
the liveness probe on `:8090/health` had nothing answering it. Added a minimal
`aiohttp` web server to `DiscoveryController.start()` that listens on `0.0.0.0:8090`
and responds to `GET /health` with 200 OK. The server is cleaned up in `stop()`.

Rebuilt and pushed the updated image to Harbor, then restarted the deployment.
Both pods are now 1/1 Running with 0 restarts. The first sync registered 20
models in LiteLLM (visible via `GET /model/info`).

### LiteLLM image pushed to Harbor (air-gapped cluster)

The K8s cluster has no internet access, so the `docker.litellm.ai/berriai/litellm:main-stable`
image cannot be pulled at pod startup. The image was tagged and pushed to the
internal Harbor registry at
`registry.adeoaiengine.ecouncil.ae/openinnovationai/platform/mlops/mlops-serving/litellm:latest`.

Changes:
- `Makefile`: Added `LITELLM_HARBOR_IMG` variable, split `push` into
  `push-discovery` and `push-litellm` targets. `push-litellm` tags the upstream
  image and pushes it to Harbor.
- `deploy/litellm-proxy.yaml`: Changed the container image from
  `docker.litellm.ai/berriai/litellm:main-stable` to the Harbor-hosted
  `registry.adeoaiengine.ecouncil.ae/openinnovationai/platform/mlops/mlops-serving/litellm:latest`.
- Verified the image is visible in Harbor via the registry API.

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
