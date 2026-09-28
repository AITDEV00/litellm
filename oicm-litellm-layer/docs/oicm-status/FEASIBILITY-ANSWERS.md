# OpenRouter Model-Status Plan — Question-by-Question Answers & Feasibility Verdict

Date: 2026-09-28
Verdict: **FEASIBLE.** Every hard blocker the plan flagged is resolved with live evidence. The only thing that could not be fully proven from data is the complete lifecycle-transition map, because the cluster snapshot contains no `Pending`/`Deploying`/`Failed` examples right now — see "What is NOT yet proven" below.

Each section answers one phase/question from the architecture plan, with the captured evidence file named inline.

## Phase 0A — IAM: is there a working identity? YES

The original blocker assumed an OICM-side permission had to be granted. That was wrong. Access to the workspace-scoped status API is keyed off the caller's **tenant**, and the tenant is the Keycloak **realm** that issues the token — no Mongo lookup at request time.

- `EntitlementService.__user_has_access_to_workspace_entity` (in `oicm-mlops-flask-be-deployment`) compares caller tenant vs workspace `_tenant_id`. `UserUtils.get_user()` reads the JWT (`flask_jwt_extended` `g.user`); `get_tenant_id()` returns the tenant from the token.
- The workspace `dfec2a9f-…` has `_tenant_id: "adeo"`. Authenticating against **realm `adeo` + client `adeo`** produces `resource_access.adeo.roles` and unlocks every status endpoint.
- No pre-existing service account existed (`user_api_key` empty; `athena_infra` is a human account; every `adeo` client has `serviceAccountsEnabled=false` so client-credentials is unavailable). We provisioned **`svc-litellm-controller`** (realm `adeo`, client `adeo`, client-roles `admin`/`default`/`rsc_groups_full_access`) via the Keycloak master admin and verified it end to end.
- `app-admin` / `oicm-admin` live in the `admin` realm/tenant and can never read `adeo` workspaces; there is no cross-realm login. They are the wrong tool for this.
- Evidence: `version.json`, the successful captures in this folder, and the verified 200s recorded in repo memory.

## Phase 0B — deployment_id == workload_id? CONFIRMED TRUE

`GET /workspaces/{ws}/deployments/{workload_id}` returns 200, the record's own `id` equals the path id, and that equals the K8s `oip/workload-id` label (`061fbb9d-c138-4800-8f69-a091fcaed7d8`). The K8s Deployment is also named `j-<workload_id>`. One UUID keys the workload, the status API path, and the K8s object. The controller passes `oip/workload-id` straight in; no separate id resolution. `OicmModel.deployment_id` can be dropped or kept as an alias.
Evidence: `deployment-detail.json`, `deployments-list.json`.

## Phase 0C — what does /inference_metrics return? CAPTURED

The earlier 500s were the same realm bug, not a telemetry outage. `inference_metrics_meta` (`inference-metrics-meta.json`) returns the metric-id vocabulary; `inference_metrics?metric_id=M&start=ISO` returns a Prometheus-style series `[[epoch_seconds, "value"], …]`.

Metric ids: `successful_requests`, `concurrent_requests`, `response_time` (unit `second`), `failed_requests`, `num_of_requests_waiting_in_queue`.

Observed data availability on a low-traffic deployment: only `num_of_requests_waiting_in_queue` had points (`metrics-queue.json`); `concurrent_requests` and `response_time` were `[]` (`metrics-concurrent.json`, `metrics-response-time.json`).

## The plan's field-mapping table — filled from live data

| Needed field | OICM has it? | Best source | Evidence |
|---|---|---|---|
| lifecycle / status | yes — `status` (`Ready`/`Stopped` observed) | OICM | `deployment-detail.json`, `deployments-list.json` |
| error detail | yes — `error_msg` | OICM | `deployment-detail.json` |
| ready / health | yes — `health.is_ready` (independent of `status`) | OICM | `deployment-health.json` |
| replicas (desired) | yes — `replicas` | OICM | `deployment-detail.json` |
| ready_replicas / per-pod state | yes — `workload_runs[].status_detail[]` | OICM | `workload-run.json` |
| queued requests | yes — `num_of_requests_waiting_in_queue` | OICM | `metrics-queue.json` |
| request rate (success/fail) | yes — `successful_requests`, `failed_requests` | OICM | meta + series |
| running/concurrent requests | yes — `concurrent_requests` (sparse) | OICM | `metrics-concurrent.json` |
| latency | partial — only **mean** `response_time`, no p50/p90/p99 | SGLang/vLLM | `metrics-response-time.json` |
| token throughput | no | SGLang/vLLM | — |
| GPU / KV utilization | no | SGLang/vLLM | — |

This confirms the plan's Phase 13 split: **OICM is the lifecycle + queue + request-count source; SGLang/vLLM is the latency-percentile + token-throughput + utilization source.**

## DTO fields — what the API actually gives

`OicmDeploymentStatusSnapshot` can be populated as follows:

- `workspace_id`, `workload_id`, `workload_run_id` — from the `oip/*` labels (already free).
- `deployment_id` — equal to `workload_id` (see 0B).
- `source_status` — `status` field.
- `healthy` — `health.is_ready`. **Do not conflate with `source_status`** — a `Ready` deployment currently reports `is_ready:false` (`deployment-health.json`).
- `replicas` — `replicas` field (desired count).
- `ready_replicas` — from `workload_runs[].status_detail[]` (per-Deployment `available_replicas`, per-Pod `ready`).
- `min_replicas` / `max_replicas` — **not available**: `auto_scaling_options` is `null` and `enable_auto_scaling` is `false` on all 28 current deployments. Drop these from the v1 DTO or make them optional.
- `observed_at` — set by the controller at fetch time.
- `status_changed_at` — **not a first-class API field.** The list item exposes `_created_at`, `_updated_at`, `_version`. Derive `status_changed_at` from the controller's own last-seen-status transition, or from `_updated_at` as an approximation. Do not assume the API provides it.
- `previous_status` — not provided by the API; must come from the controller's stored last snapshot (which the plan already persists in `model_info.oicm`).

## State machine — what is proven vs assumed

Observed `status` values in this snapshot: `Ready` ×24, `Stopped` ×4. No `Pending`/`Deploying`/`Failed` present.

Proven from data:
- `Ready` exists and coexists with `is_ready:false` (so `Ready` ≠ serving-healthy).
- `Stopped` exists.
- The `events` SSE stream carries the transition vocabulary needed for the `restarting`/`redeploying` derivation: `ScalingReplicaSet`, `Killing`, `SuccessfulCreate`, `SuccessfulDelete`, `Unhealthy`, `BackOff`, `FailedScheduling`, `TaintManagerEviction`, `Pulled`/`Pulling`/`Started`/`Created` (`workload-run-events.sse`, 81 Pod + 8 ReplicaSet + 7 Deployment + 3 PVC events).

Assumed, not yet observed in this snapshot:
- `Pending`, `Deploying`, `Failed` status strings. The plan's transition table (`Pending→starting`, `Ready→Deploying→redeploying`, `Ready→Failed→…→restarting`) cannot be fully validated until a deployment actually cycles. The `workload_run_id` change and the events stream give the signals; the exact `source_status` strings for the transitional phases should be confirmed the first time a real rollout is observed.

## Recommended `source_status` → gateway_status mapping (to validate on first live rollout)

```
Ready     + is_ready=true   -> availability=online,  lifecycle=stable
Ready     + is_ready=false  -> availability=degraded,lifecycle=stable   (ready but not serving)
Stopped                     -> availability=offline, lifecycle=stopped
Deploying + never Ready     -> availability=offline, lifecycle=deploying (starting)
Deploying + was Ready       -> availability=online,  lifecycle=deploying (redeploying)
Failed                      -> availability=offline, lifecycle=failed
stale observation           -> availability=unknown, stale=true
```

## What is NOT yet proven (the honest gap)

1. The transitional status strings (`Pending`/`Deploying`/`Failed`) — none exist in the current snapshot, so the strings are inferred from the events vocabulary and the backend constant names, not from a captured `status` value. Confirm on the next real rollout.
2. `num_of_requests_waiting_in_queue` semantics — the plan correctly warned not to assume `Queued` == SGLang scheduler depth. The metric exists and has data, but whether it reflects the engine's internal queue or OICM's gateway queue is unconfirmed. Treat it as a generic queue-depth signal until verified against a known-loaded deployment.
3. `concurrent_requests` and `response_time` were empty on this deployment, so their time-resolution/step behavior under load is uncharacterized.

## Evidence bundle contents

| File | What it proves |
|---|---|
| `version.json` | platform version 1.15.0 build 8e7f0bca |
| `deployments-list.json` | 28 deployments, status enum, full list-item schema |
| `deployment-detail.json` | id==workload_id, status/error_msg/replicas/urls |
| `deployment-health.json` | `is_ready` independent of `status` |
| `inference-metrics-meta.json` | the five metric ids + units |
| `metrics-queue.json` | queue-depth series (has data) |
| `metrics-concurrent.json` | empty series (sparse) |
| `metrics-response-time.json` | empty series (mean latency only) |
| `workload-run.json` | richest per-run `status_detail[]` |
| `workload-run-events.sse` | transition event vocabulary |
| `FEASIBILITY-ANSWERS.md` | this document |
| `OICM-STATUS-FEASIBILITY.md` | the full study (route table, IAM mechanism, method) |

No credentials are included in this bundle.
