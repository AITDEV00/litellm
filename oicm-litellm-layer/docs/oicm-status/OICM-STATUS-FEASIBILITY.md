# OICM Deployment-Status Integration — Feasibility Study

Date: 2026-09-28 (updated same day after IAM resolution)
Author: investigation run against the live OICM platform (`https://oicm.adeoaiengine.ecouncil.ae`, version `1.15.0`, build `8e7f0bca`)
Credentials used: `svc-litellm-controller` (dedicated service user, Keycloak realm `adeo`, client `adeo`)

This document answers the open questions in the gateway-status architecture plan ("Phase 0 — resolve the three remaining blockers") with live API evidence, and states what is and is not currently buildable.

## TL;DR

The plan's central assumption — that the controller can read OICM deployment status and inference metrics over REST — **holds**. All three Phase 0 blockers are resolved:

1. **IAM**: the block was never a permission to grant in OICM's data model, it was authenticating against the wrong Keycloak realm. The tenant is the realm: an `adeo`-tenant identity is any user in the `adeo` realm authenticating with client `adeo`. We provisioned a dedicated service user `svc-litellm-controller` in that realm and verified read access on every status endpoint.
2. **`deployment_id == workload_id`: confirmed true.** The deployment record's `id` equals the path id equals the K8s `oip/workload-id` label. No separate id resolution is needed.
3. **Metrics shape: captured.** Five metric ids, Prometheus-style `[[epoch,"value"],…]` series. Data is thin for low-traffic deployments, so OICM metrics are a secondary source; latency/throughput for `gateway_status` should come from SGLang/vLLM directly.

The OpenRouter `/endpoints` `gateway_status` work can therefore be built against the OICM REST status path directly (`OicmClient`), with the K8s-derived snapshot kept only as a freshness/fallback source.

## Method

Token obtained via the tenant's own realm and client (this pairing is the crux — realm and client must both equal the tenant name):

```
POST {AUTH_BASE_URL}/realms/adeo/protocol/openid-connect/token
  client_id=adeo  username=svc-litellm-controller  password=***  grant_type=password  scope=openid
```

The earlier failed attempts used the `admin` realm + `admin` client. That pairing issues a token whose `resource_access` carries the `admin` tenant, which never matches a workspace whose `_tenant_id` is `adeo`. See the IAM section below for the exact mechanism.

A series of authenticated and unauthenticated `curl` probes established the route table. The unauthenticated probe is the key diagnostic: on this server a **401/403 means the route exists**, while a **404 can mean either "no route" or "route exists but the resource is hidden from this principal"**. Comparing the authed and unauthed status codes for the same path disambiguates the two.

Real correlation IDs were taken from a live OICM-created Kubernetes Deployment (`j-061fbb9d-...`, namespace `adeo`):

```
oip/workspace-id     = dfec2a9f-cc5c-4b7b-b608-990d3804e80c
oip/workload-id      = 061fbb9d-c138-4800-8f69-a091fcaed7d8
oip/workload-run-id  = b2dff57d-ce61-426c-a917-2b49bcbb319c
oip/workload-type    = model_deployment
oip/author-username  = jyao
```

All `jyao` deployments in the `adeo` namespace share the single workspace `dfec2a9f-cc5c-4b7b-b608-990d3804e80c`.

## API surface discovered

No machine-readable schema is served: `/openapi.json`, `/docs`, `/redoc` all return the SPA fallback (HTTP 200 with a "not found" body), and the `/api/**` variants are plain 404. The route table below was established empirically.

### Confirmed routes

| Route | Auth behaviour | Notes |
|---|---|---|
| `GET /api/v1/version` | 200 | `{"version":"1.15.0","build":"8e7f0bca","dev_license":true}` |
| `GET /api/v1/model_servers/summary` | 401 unauthed; 200 authed | Returns `{data:[...]}`, 20 entries. Fields: `id,name,tag,family,enabled,devices,supported_devices,task_types,multinode_inference_support,supported_auto_scaling_metrics` |
| `GET /api/v1/model_servers/{id}` | 200 | Full server record incl. `server_arguments`, `inference_arguments`, `tasks_base_url`, etc. |
| `POST /api/v1/model_servers` | collection is `allow: POST, OPTIONS` | Onboarding path. Write access confirmed working (400 on a deliberately malformed body, not 403) |
| `GET /api/v1/workspaces/{ws}/deployments` | 403 unauthed; **200 as svc-litellm-controller** | `{items:[…]}`, 28 entries; statuses `Ready`×24, `Stopped`×4 |
| `GET /api/v1/workspaces/{ws}/deployments/{id}` | 403 unauthed; **200 as svc-litellm-controller** | Full record. `id` in body == path id == K8s `oip/workload-id`. Carries `status`, `error_msg`, `replicas`, `model_server`, `resources`, `inference_url`, `internal_inference_url` |
| `GET /api/v1/workspaces/{ws}/deployments/{id}/health` | 403 unauthed; **200 as svc-litellm-controller** | `{is_health_check_supported, is_ready, message}`. NOTE: `is_ready` is independent of `status` — a `Ready` deployment can report `is_ready:false` |
| `GET /api/v1/workspaces/{ws}/deployments/{id}/inference_metrics` | 403 unauthed; **200 as svc-litellm-controller** | Requires `metric_id` + `start`. Returns `[[epoch_seconds,"value"],…]`. Empty `[]` when no traffic |
| `GET /api/v1/workspaces/{ws}/deployments/{id}/inference_metrics_meta` | 403 unauthed; **200 as svc-litellm-controller** | `{metrics:[{id,title,unit?}…], range_start}`. Metric ids below |
| `GET /api/v1/workspaces/{ws}/workloads/{wid}` | 401/403 unauthed; **404 even as svc-litellm-controller** | Route exists but the bare workload record is hidden; use `deployments` + `workload_runs` instead |
| `GET /api/v1/workspaces/{ws}/workloads/{wid}/workload_runs/{rid}` | 401 unauthed; **200 as svc-litellm-controller** | Richest per-run status: `status_detail[]` breaks down Deployment/Pod/PVC with `available_replicas`, `ready`, node, per-kind `status_msg`; plus `workload_status`, `resources`, `user_id` |
| `GET /api/v1/workspaces/{ws}/workloads/{wid}/workload_runs/{rid}/events` | 401 unauthed; **200 as svc-litellm-controller** | `text/event-stream` (SSE); `data:{…k8s event…}` lines with `event_reason`, `event_type`, `first_seen`/`last_seen`. Best source for transition detection |

### Confirmed non-routes (404 both with and without auth)

`/api/v1/deployments/{id}`, `/api/v1/deployments/{id}/health`, `/api/v1/workspaces/{ws}/model_deployments`, `/api/v1/workspaces/{ws}/workloads/{wid}/runs/{rid}`, `/api/v1/workspaces/{ws}/workload_runs/{rid}`, `/api/v1/models`, `/api/v1/users/me`, `/api/v1/me/workspaces`, `/api/v1/workspaces/{ws}/members`, and the bare nouns `/api/v1/{workloads,workspaces,deployments,clusters,nodes,gpus,metrics,tasks,runs,endpoints,providers,registries,images,keys,users,tenants,inference}`.

The single-workspace status API is the `/api/v1/workspaces/{ws}/deployments/{id}/...` family, plus `GET /api/v1/workspaces/{ws}/deployments` (list, confirmed 200 — see `evidence/deployments-list.json`). There is no cross-workspace deployment list endpoint, which matters for the controller design (see below).

## Phase 0 question A — is IAM fixed?

**Yes — by authenticating against the correct Keycloak realm, not by granting anything in OICM.** The status API keys access off the caller's *tenant*, and the tenant is the Keycloak realm that issued the token.

`EntitlementService.__user_has_access_to_workspace_entity` (in `oicm-mlops-flask-be-deployment`) compares the caller's tenant against the workspace's `_tenant_id`. `UserUtils.get_user()` reads the user from the JWT (`flask_jwt_extended` `g.user`), and `get_tenant_id()` returns the tenant baked into the token by its issuing realm — there is no Mongo lookup at request time.

The workspace `dfec2a9f-…` has `_tenant_id: "adeo"`. Our first attempts used the `admin` realm + `admin` client, whose token carries `resource_access.admin` — never matching `adeo`. Authenticating against realm `adeo` + client `adeo` produces a token with `resource_access.adeo.roles`, which matches, and every status endpoint returns 200.

No pre-existing service account existed, so we provisioned a dedicated one via the Keycloak master admin:

```
realm = adeo   client = adeo   username = svc-litellm-controller
client-role mappings on the "adeo" client: admin, default, rsc_groups_full_access
```

This mirrors the working `jyao` mapping exactly. Verified 200 on all six status endpoints. (Every client in the `adeo` realm has `serviceAccountsEnabled: false`, so client-credentials is unavailable in this realm — password grant is the only machine flow, which is fine since `OicmClient` will encapsulate the grant type.)

## Phase 0 question B — is `deployment_id == workload_id`?

**Confirmed true.** `GET /workspaces/{ws}/deployments/{workload_id}` returns 200, and the record's own `id` field equals the path id, which equals the K8s `oip/workload-id` label (`061fbb9d-…`). The OICM-created Kubernetes Deployment is also named `j-<workload-id>`. One UUID keys the workload, the status API's `{id}` path parameter, and the K8s object.

The controller can pass `workload_id` (from the `oip/workload-id` label) straight into the status path. `OicmModel` does not need a separately-resolved `deployment_id`; the field can be dropped or kept as an alias of `workload_id`.

## Phase 0 question C — what does `/inference_metrics` actually return?

**Captured.** The earlier 500s were the same realm bug, not a telemetry outage. `inference_metrics_meta` returns:

```json
{
  "metrics": [
    {"id": "successful_requests",              "title": "Successful Requests"},
    {"id": "concurrent_requests",              "title": "Concurrent Requests"},
    {"id": "response_time",                    "title": "Response Time", "unit": "second"},
    {"id": "failed_requests",                  "title": "Failed Requests"},
    {"id": "num_of_requests_waiting_in_queue", "title": "Waiting Requests in Queue"}
  ],
  "range_start": "2026-09-21T12:13:28.549000Z"
}
```

`inference_metrics?metric_id=M&start=ISO` returns a Prometheus-style series `[[epoch_seconds, "value"], …]`. For this low-traffic deployment only `num_of_requests_waiting_in_queue` had points; the rest were `[]`.

Field-mapping consequence: OICM gives request-count and queue-depth series, but **no latency percentiles, token throughput, or GPU/KV utilization**. So the plan's Phase 13 split is confirmed, with OICM as the lifecycle/queue source and SGLang/vLLM as the latency/throughput/utilization source:

| Needed field | OICM has it? | Best source |
|---|---|---|
| lifecycle / status | yes (`status`, `error_msg`) | OICM |
| ready/health | yes (`health.is_ready`) | OICM |
| replicas | yes (`replicas`, `status_detail[].metadata`) | OICM |
| queued requests | yes (`num_of_requests_waiting_in_queue`) | OICM |
| request rate / success / fail | yes (`successful_requests`, `failed_requests`) | OICM |
| running/concurrent requests | yes (`concurrent_requests`) | OICM |
| latency p50/p90/p99 | **no** (only mean `response_time`) | SGLang/vLLM |
| token throughput | **no** | SGLang/vLLM |
| GPU / KV utilization | **no** | SGLang/vLLM |

## What this means for the plan

- **Phase 0A (IAM): resolved.** A dedicated `adeo`-tenant service user reads all six status endpoints. Nothing is gated on the platform team for reads.
- **Phase 0B (id identity): resolved.** `deployment_id == workload_id`. The controller passes the `oip/workload-id` label value directly; `OicmModel` needs no separate id resolution.
- **Phase 0C (metrics shape): resolved.** Five metric ids and a Prometheus-style series, but no latency percentiles / token throughput / GPU-KV data — those still require SGLang/vLLM.
- The event-driven K8s trigger (Phase 4) remains the right primary mechanism; the OICM status read is the enrichment step it triggers.
- The `model_info.oicm` persistence design (Phase 6) and the availability/lifecycle split (Phase 7) can now source from OICM directly (`source: "oicm"`), with K8s as fallback.
- The telemetry layer (Phases 12–15) ships with an OICM provider for lifecycle + queue depth and an SGLang/vLLM provider for latency/throughput/utilization.
- The bare `GET /workloads/{wid}` route stays hidden even to an adeo member; use `deployments` + `workload_runs` instead of relying on it.

## Recommended first milestone

Build `gateway_status` sourced from OICM via the new service identity:

```
K8s watch event (oip/* labels)          # trigger + correlation ids
        ->
OicmClient.get_deployment(workload_id)  # realm=adeo, client=adeo, svc-litellm-controller
   + get_deployment_health(workload_id)
   + get_workload_run(workload_run_id)  # richest per-run status_detail[]
        ->
OicmDeploymentStatusSnapshot {
    workspace_id, workload_id, workload_run_id,   # from labels
    source: "oicm",
    source_status: status,        # Ready / Stopped / ...
    error_msg,
    healthy: health.is_ready,     # independent signal, do NOT conflate with status
    replicas,
    observed_at
}
        ->
model_info.oicm  +  gateway_status { availability, lifecycle }
```

Two payload nuances to encode in the status model: `health.is_ready` is independent of `status` (a `Ready` deployment can report `is_ready:false`), and `workload_runs/{rid}` is the richest per-run source (`status_detail[]` per Deployment/Pod/PVC), with the `events` SSE stream best for transition detection (starting vs redeploying vs restarting).

## Open items for the platform team

None blocking reads. Two clarifications remain useful but non-gating:

1. Confirm the `Ready` / `Queued` field semantics before anything maps them to `running_requests` / `queued_requests` in the public API.
2. If a client-credentials (service-account) grant is ever preferred over password grant, enable `serviceAccountsEnabled` on a dedicated client in the `adeo` realm; today every client there has it disabled, so password grant is the only machine flow. `OicmClient` should keep the grant type swappable either way.
