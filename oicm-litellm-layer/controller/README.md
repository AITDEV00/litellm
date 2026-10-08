# OICM Discovery Controller

A Kubernetes sidecar that discovers model deployments (local and cross-cluster via Submariner) and
registers them as LiteLLM models through the LiteLLM proxy REST API.

## What This Service Does

The controller runs inside the cluster and watches for model-serving workloads. When it finds one,
it registers it in the LiteLLM proxy so that clients can route requests through LiteLLM's unified
endpoint (`/chat/completions`, `/embeddings`, etc.) instead of talking to each model server
directly.

It discovers models from two sources:

1. **Local deployments**. Kubernetes Deployments in the `adeo` namespace labeled with
   `oip/workload-type=model_deployment`. The controller reads the `MODEL_ID` from a ConfigMap or
   queries the model server's `/v1/models` endpoint, then registers it in LiteLLM with an
   `api_base` pointing at the in-cluster service (`s-{uuid}.adeo.svc.cluster.local:8080`).

2. **Submariner cross-cluster imports**. EndpointSlices created by the Submariner Lighthouse agent
   that carry the `oip/workload-type=model_deployment` label. These represent services exported
   from remote clusters (e.g. Abu Dhabi) and made reachable via Submariner globalnet IPs. The
   controller queries each remote model's `/v1/models` endpoint through the globalnet IP and
   registers it in LiteLLM with an `api_base` pointing at the globalnet IP
   (`http://242.0.0.x:8080/v1`).

Both sources are polled on every full sync cycle (default every 300 seconds). Local deployments are
also watched in real-time via the Kubernetes watch API for immediate add/delete/modify events.

## Architecture (VSA)

The code follows vertical slice architecture. Each source is a self-contained module implementing
the `ModelSource` ABC. The controller orchestrates them without knowing their internals.

```
controller/
  __main__.py              Entry point. Signal handling, event loop.
  config.py                Environment variables, constants, logging setup.
  models.py                OicmModel dataclass, sanitize_model_id, detect_mode.
  litellm_client.py        LiteLLMClient. Batch register/deregister/patch via REST API.
  reconciler.py            SyncReconciler. Pure compute_plan + execute. Dedup logic.
  exclusions.py            Excluded model ids. Declarative slice, like sources_config.
  controller.py            DiscoveryController. Orchestration: start/stop, full_sync,
                           watch loop, event handlers, health endpoint.
  fallbacks/
    __init__.py            Exports FallbackReconciler.
    client.py              FallbackClient. Reads LiteLLM fallback config.
    service.py             FallbackReconciler. Validates existing fallbacks.
  sources/
    __init__.py            Exports ModelSource.
    base.py                ModelSource ABC. Single method: discover() -> Dict[str, OicmModel].
    local_deployments.py   LocalDeploymentSource. Watches K8s Deployments + ConfigMaps.
    submariner_imports.py  SubmarinerImportSource. Reads lighthouse EndpointSlices.
```

### Dependency graph (strictly one-directional, no cycles)

```
__main__ -> controller -> sources (base, local_deployments, submariner_imports)
                        -> reconciler -> litellm_client -> models
                        -> fallbacks (service -> client)
                        -> models, config
sources -> models, config
```

### How to add a new source

1. Create a new file in `sources/` (e.g. `sources/my_source.py`).
2. Implement the `ModelSource` ABC with a `discover()` method that returns
   `Dict[str, OicmModel]`.
3. Add it to `DiscoveryController.__init__` or inject it via the `sources` parameter.

No other code needs to change. The reconciler, LiteLLM client, and fallback logic all operate on
`OicmModel` objects and are source-agnostic.

## How Submariner Import Discovery Works

Submariner's Lighthouse agent creates EndpointSlice objects in the destination cluster for every
ServiceExport in a remote cluster. These EndpointSlices are labeled with:

- `endpointslice.kubernetes.io/managed-by=lighthouse-agent.submariner.io`
- `multicluster.kubernetes.io/source-cluster` (e.g. `abudhabi`)
- `multicluster.kubernetes.io/service-name` (the original service name)
- `oip/workload-type=model_deployment` (propagated from the source service labels)
- `oip/workload-id` (propagated from the source service labels, if present)

The EndpointSlice's `endpoints[].addresses` field contains the globalnet IP (e.g. `242.0.0.253`),
which is routable from any node in the local cluster through the Submariner tunnel.

The `SubmarinerImportSource` lists all EndpointSlices in the `adeo` namespace matching the
lighthouse label and the `oip/workload-type=model_deployment` label. For each one, it:

1. Extracts the globalnet IP and port from the EndpointSlice.
2. Queries `http://{globalnet_ip}:{port}/v1/models` to discover the model ID.
3. Constructs a composite UUID: `submariner:{source_cluster}:{workload_id}`.
4. Creates an `OicmModel` with `source="submariner:{cluster}"` and
   `api_base_override="http://{globalnet_ip}:{port}/v1"`.
5. Returns it to the controller for reconciliation.

The composite UUID ensures that models from different clusters never collide, even if they share
the same workload ID. The `model_name` is prefixed with the source cluster name (e.g.
`abudhabi-Qwen3.5-0.8B`) so it is distinguishable in LiteLLM.

### Extensibility

Adding a third Submariner-connected cluster requires zero code changes. When the new cluster's
services are exported and Submariner creates EndpointSlices for them in the local cluster, the
`SubmarinerImportSource` will discover them automatically on the next sync cycle. The
`multicluster.kubernetes.io/source-cluster` label distinguishes models from different clusters, and
the composite UUID format `submariner:{cluster}:{id}` prevents collisions.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `LITELLM_ADMIN_URL` | `http://localhost:4000` | LiteLLM proxy admin API URL |
| `LITELLM_ADMIN_KEY` | (from `deploy/prod/litellm-proxy.yaml` `litellm-master-key` secret) | LiteLLM master key |
| `WATCH_NAMESPACE` | `adeo` | Namespace to watch for model deployments and EndpointSlices |
| `CLUSTER_DOMAIN` | `svc.cluster.local` | Kubernetes cluster domain for local service DNS |
| `MODEL_PORT` | `8080` | Port that model servers listen on |
| `SYNC_INTERVAL` | `300` | Seconds between full sync cycles |
| `WATCH_TIMEOUT` | `300` | Kubernetes watch timeout in seconds |
| `HEALTH_PORT` | `8090` | HTTP health check server port |
| `HTTP_CONCURRENCY` | `50` | Max concurrent HTTP requests to LiteLLM API |
| `ENABLE_SUBMARINER_IMPORTS` | `true` | Enable Submariner cross-cluster import source |
| `STATUS_SYNC_INTERVAL` | `10` | Seconds between OICM status polls, per source |
| `OICM_SOURCES_FILE` | `/etc/oicm/sources.yaml` | Path to the OICM source definitions |
| `OICM_EXCLUSIONS_FILE` | `/etc/oicm-exclusions/exclusions.yaml` | Path to the excluded-model definitions |
| `OICM_EXCLUDED_MODEL_IDS` | (unset) | Comma-separated model ids to exclude, merged with the file |

## OICM status sources

Which OICM instances the controller reads deployment status from is declared in a
ConfigMap, `deploy/oicm/sources.yaml`, mounted at `/etc/oicm`. One entry per
instance, carrying its endpoints, realm, client, and workspace. Adding a cluster
is a data change, not a code change.

The same file is read directly by a local run, where the ConfigMap envelope is
unwrapped, so there is one copy rather than two that can drift.

Credentials are deliberately not in that file. A ConfigMap cannot hold or
interpolate a Secret value, so each source's credentials are wired into the
Deployment from that source's own Secret, under variable names derived from the
source name:

| Source name | Credential variables |
|---|---|
| `alain` | `OICM_SOURCE_ALAIN_USERNAME`, `OICM_SOURCE_ALAIN_PASSWORD` |
| `abudhabi` | `OICM_SOURCE_ABUDHABI_USERNAME`, `OICM_SOURCE_ABUDHABI_PASSWORD` |

Any field in the file can be overridden per deployment with
`OICM_SOURCE_<NAME>_<FIELD>`, for example `OICM_SOURCE_ALAIN_WORKSPACE_ID`. A
source listed in the ConfigMap whose credentials are absent is skipped with a
warning, so a partial rollout degrades to fewer sources rather than failing.

Both clusters are read at once. Sources are polled concurrently, so a cycle costs
the slowest source rather than the sum, and a source that fails retains its own
last snapshots while the others still land. `GET /status` reports each
snapshot's `source`.

Both OICM versions the controller talks to (Al Ain `1.15.19` and Abu Dhabi
`1.7.1`) use the same `OicmStatusSource`; they differ only in whether
`status_detail[]` carries `metadata`, which the shared availability logic treats
as optional.

## Excluding models

Models that must never appear on the gateway are declared per environment, so a
model can be excluded in one and kept in the other. Each controller mounts only
its own ConfigMap at `/etc/oicm-exclusions`:

| Environment | File | ConfigMap |
|---|---|---|
| prod | `deploy/oicm/exclusions.yaml` | `oicm-exclusions` |
| dev | `deploy/dev/oicm-exclusions-dev.yaml` | `oicm-exclusions-dev` |

Exclusion is stronger than the `blocked` routing flag: an excluded deployment is
never registered, and one that is already registered is deleted, so it is absent
from `/v1/models` rather than merely unroutable.

```yaml
model_ids:
  - Qwen/Qwen3.6-35B-A3B-FP8
  - 766b1720-f516-4077-b22c-6ce97c045470
```

An entry matches any of a deployment's identities: the served model id, the
gateway's sanitized name (slashes as `--`), or the deployment uuid. Prefer the
served model id, because it is stable across redeploys while the uuid is
regenerated each time. The uuid is the only handle for a Stopped deployment,
whose served id cannot be discovered.

The controller reads the file once at startup, so an edit plus a re-apply of the
ConfigMap takes effect on the next controller restart. Reading it once rather
than every sync is deliberate: a transiently unreadable file would otherwise
yield an empty set and re-register every excluded model. The
`OICM_EXCLUDED_MODEL_IDS` env var is merged with the file for a temporary
exclusion. A malformed file yields an empty set rather than raising: exclusion is
a safety net, and the worst case of missing it is a model that stays registered,
whereas raising would stall the controller.

## OicmModel Fields

| Field | Description |
|---|---|
| `uuid` | Unique identifier. For local: the workload UUID. For Submariner: `submariner:{cluster}:{id}` |
| `deployment_id` | The reconciler's key for this model. Equal to `uuid`: one deployment is one model, and the uuid is the only identity OICM and the gateway reliably share. Never the model name, which OICM stores as a GUI label |
| `model_id` | The model ID returned by `/v1/models` or read from ConfigMap |
| `model_name` | Sanitized name for LiteLLM. For Submariner imports, prefixed with cluster name |
| `namespace` | Kubernetes namespace |
| `ready_replicas` | Number of ready replicas (local deployments only; Submariner imports always 1) |
| `total_replicas` | Total replicas (local deployments only) |
| `mode` | `chat`, `embedding`, `transcription`, or `tts_skip` |
| `source` | `local` or `submariner:{cluster_name}` |
| `api_base_override` | For Submariner imports: `http://{globalnet_ip}:{port}/v1`. For local: `None` (computed from uuid) |
| `extra_args` | Extra args from ConfigMap (used for mode detection) |

## LiteLLM Model Info Tags

Each registered model is tagged with `model_info` metadata in LiteLLM:

```json
{
  "mode": "chat",
  "oicm_uuid": "submariner:abudhabi:766b1720-f516-4077-b22c-6ce97c045470",
  "oicm_cluster": "abudhabi",
  "oicm_source_name": "abudhabi",
  "oicm_namespace": "adeo",
  "oicm_source": "submariner:abudhabi",
  "oicm": {
    "v": 1,
    "status": "Ready",
    "serving_available": true,
    "gateway_uuid": "766b1720-f516-4077-b22c-6ce97c045470",
    "replicas": {"desired": 1, "available": 1},
    "status_changed_at": "2026-10-06T00:00:00+00:00",
    "observed_at": "2026-10-06T00:00:00+00:00",
    "error_msg": null
  }
}
```

`oicm_cluster` says which cluster the deployment physically runs in: `alain` for
a local
deployment, the source cluster for a cross-cluster import. It is what answers
"Abu Dhabi or Al
Ain" for a model without anyone parsing a uuid. `CLUSTER_NAME` supplies it for a
local
deployment and is required, not defaulted, because a guessed cluster is a wrong
answer that
looks authoritative.

`oicm_source_name` says which configured OICM reported the status, taken from the
`name:` in
`sources.yaml`. It is separate from `oicm_cluster` because the two only coincide
while the
gateway runs in the same cluster as one of its sources. A consumer looks up
freshness by
cluster and ownership by source, so both are recorded.

`oicm_source` records how the row was discovered: `"local"` for a deployment
watched in this
cluster, `"submariner:abudhabi"` for a cross-cluster import. The `oicm` block is
the OICM
lifecycle status, written by the status poller on its own 10s clock.

`oicm_uuid` keeps the transport prefix that a Submariner import needs for
uniqueness. The
`deployment_id` the reconciler keys on, and the `gateway_uuid` in the status
block, both strip
it, so an import and its own OICM's snapshot land on one key. The reconciler's
delete rule is
scoped to rows carrying an `oicm_uuid` and gated on a complete poll, so an
admin-added model
is never deleted, and an unreachable OICM is never read as deletion.

Liveness and health are no longer model rows. The controller POSTs per-model
health to
`/oicm/v1/status-reports` (on change, else hourly refresh) and per-source
liveness to
`/oicm/v1/heartbeats` (every 30s), both landing in the native
`LiteLLM_HealthCheckTable` with server-stamped `checked_at` and
`checked_by = oicm-controller`. Consumers read them through the native
`/health/latest`, `/health/history`, and the Admin UI health column; a source's
freshness is the `checked_at` of its `oicm-source-<cluster>` row. Retention is
the native `maximum_health_check_retention_period` knob.

## Running

```bash
# Run the controller (continuous loop with watch + periodic resync)
python -m controller

# Run a single sync cycle and exit
python -m controller --once
```

## RBAC Requirements

The controller's ServiceAccount needs permissions in the `adeo` namespace:

- `apps/deployments`: get, list, watch (local deployment discovery)
- `services`: get, list (health checks)
- `configmaps`: get, list (MODEL_ID discovery)
- `discovery.k8s.io/endpointslices`: get, list, watch (Submariner import discovery)

See `deploy/prod/discovery-controller.yaml` for the full manifest.
