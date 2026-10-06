# Model status persistence in LiteLLM `model_info`

Date: 2026-10-06
Status: design agreed, not yet implemented
Scope: how the controller stores OICM deployment status on the LiteLLM model row, and how a
stopped deployment stays visible while becoming unroutable

## The problem

A model deployment that OICM reports as `Stopped` currently disappears from the LiteLLM
gateway entirely, so `Stopped` and `Deleted` are indistinguishable to any consumer.

Verified on dev against deployment `93457b11-bf43-4ba0-bcbe-ba00624857ff`:

```
OICM status        : Stopped
OICM replicas      : 1                 <- stale, no pods exist
OICM status_detail : ()                <- empty
k8s Deployment     : does not exist
LiteLLM entries    : 0
```

OICM retains the record. It still exposes the identity:

```
model_name      = Qwen/Qwen3.5-0.8B
deployment_type = Model Registry
```

but there is no k8s Deployment and no Service, so the controller cannot re-probe
`/v1/models` and cannot resolve a ClusterIP for it.

Evidence: `evidence/stopped-deployment.json`.

## Source of truth for existence

The reconciler currently treats absence from the k8s watch as deletion, which is only safe
while OICM and k8s agree. Once they diverge, existence has to come from OICM, and the two
states must be told apart explicitly:

| In OICM | In k8s | Meaning | Action |
|---|---|---|---|
| yes | yes | healthy or deploying | keep registered, refresh `api_base` and status |
| yes | no | `Stopped` / `Failed`, lifecycle state | **keep registered**, set status, block routing |
| no | n/a | deleted | remove the row |

This is the Kubernetes EndpointSlice model. A terminating pod is not removed from the
EndpointSlice; it stays listed with `terminating: true` and `serving: false`, and is dropped
only when it is actually gone. The endpoint object carries a lifecycle state rather than
being present-or-absent.

### Do not add a `deleted` or `is_active` flag

`status` already is the lifecycle state. `Stopped` is a legitimate member of that lifecycle,
not a marker meaning "treat as absent". Adding a boolean delete marker alongside it creates
two sources of truth that can disagree, and the well-documented failure mode of soft-delete
flags is exactly that they get treated inconsistently at different call sites.

The consequence is that deletion must be signalled by **absence from OICM's
`deployment_summary`**, not by a status value. That is the discriminator in the table above.

## Visibility is not routing

Keeping a stopped model registered while leaving it routable is worse than deleting it:
LiteLLM will still select it and every request fails with a connection error because the
Service is gone.

Two layers, mirroring the EndpointSlice split:

- **Visibility**: `model_info.oicm.status = Stopped`. The model appears in `/models` and
  `/endpoints` with `gateway_status.availability = offline`.
- **Routing**: the deployment is excluded from selection.

LiteLLM already has the routing primitive. `Router` filters on `model_info.blocked` in nine
places, and `router.py:10353` documents it as the paused-deployment flag.

### Decision: the controller owns `blocked` for rows it manages

`blocked` is currently an admin action and the patch endpoint enforces that only a proxy
admin can change it. The master key qualifies, so the controller can set it.

Adopting it wholesale would mean the controller clears a block a human set deliberately, and
a human unblock could make a stopped model routable. The rule is therefore:

- The controller manages `blocked` only on rows carrying `model_info.oicm`, which is exactly
  the set it created.
- It sets `blocked = true` when the deployment is not serving, and `false` when it is serving.
- It records the reason so a human can tell an OICM block from an admin pause.

## What to store

Because a stopped deployment cannot be re-probed, the block has to persist the discovery that
produced the row. Otherwise the row is invalid the moment the deployment stops.

Stored in `model_info.oicm`:

| Field | Why |
|---|---|
| `v` | shape version; the block is replaced wholesale, so a future change needs a discriminator |
| `status` | the OICM lifecycle word |
| `serving_available` | separates Ready-and-serving from Ready-but-degraded |
| `api_base` | **must persist**; cannot be re-probed once stopped |
| `replicas: {desired, available}` | explains `Deploying` and degraded without a second lookup |
| `status_changed_at` | "stopped for N minutes", and the serving-flip-within-Ready case |
| `observed_at` | the only way a consumer computes staleness |
| `error_msg` | explains a `Failed`; length-capped before storing |

Dropped: `workspace_id` (constant, implied by controller config), `source_updated_at`
(documented as unreliable), `unavailable_replicas` (derivable), `workload_id` (already
present as `oicm_uuid`).

Existing sibling keys `oicm_uuid`, `oicm_namespace`, `oicm_source` stay as they are.

## How to upsert

Four mechanics constrain the write, all verified.

1. **No bulk endpoint.** `/model/new`, `/model/{id}/update`, and `/model/delete` are all
   per-id. A deployment hosting three models is three writes.
2. **`model_info` merges shallowly.** `update_db_model` performs
   `merged_model_info.update(patch.model_info)`, so sending `{"oicm": {...}}` replaces the
   whole `oicm` object and leaves the other 141 keys alone. Always send the complete block.
3. **Every write triggers a full reload.** One PATCH produced eight reload warnings on the
   pod, because the endpoint calls `clear_cache()`, which reloads every DB model and fans out
   to other replicas over Redis with a 10s minimum resync interval.
4. **Every write bumps `model_info.updated_at`**, and `upsert_deployment` compares
   `model_info` for equality, so every write is a real deployment swap, never a no-op.

Therefore:

- **Read-modify-write, with a free read.** `list_all_models_by_key` already fetches
  `/model/info`, which includes `model_info`, so the current block is in hand while the plan
  is computed. No extra call.
- **Gate on change, excluding `observed_at`.** Including `observed_at` in the comparison
  makes every cycle differ, so every cycle writes.
- **`observed_at` is a heartbeat on its own cadence**, derived from `STATUS_STALE_AFTER`
  rather than from the poll interval. Otherwise a 10s poll is six full reloads a minute per
  pod to advance a timestamp.
- **`api_base` is refreshed by the k8s watch while the deployment exists**, so a restart
  transition refreshes it before the row becomes routable again.

## The pre-existing guard this depends on

`compute_plan` currently appends a patch for every matched key with no comparison against the
existing entry, so all 25 models are PATCHed every 300s whether or not anything changed. That
is 7,200 gateway writes a day of pure churn, and it is the same waste at larger scale than the
status writes.

Adding the idempotence guard is a prerequisite, and it is independent of this design and
testable on its own.

## Implementation order

1. Idempotence guard in `compute_plan` (compare the computed patch to the existing entry,
   skip when equal). Independent, safe, removes the existing churn.
2. Existence keyed on OICM `deployment_summary`: register when present in OICM, delete only
   when absent from it. Bypass the `if not model.is_ready: continue` gate in `compute_plan`
   for deployments present in OICM but not serving.
3. `patch_model_info` writing the whole `model_info.oicm` block, called only when the block
   differs, excluding `observed_at`.
4. Controller ownership of `blocked`, scoped to rows carrying `model_info.oicm`, with the
   reason recorded.
5. `observed_at` heartbeat on a cadence derived from `STATUS_STALE_AFTER`.
6. Parallelize `LocalDeploymentSource.discover()`, which currently awaits each deployment
   serially (24 deployments in about 2.6s), so a faster cadence is affordable.

Steps 1 and 5 are what make a shorter poll interval safe rather than harmful.

## Testing

- Idempotence: a second identical cycle issues zero PATCHes.
- Status change: a `Ready` to `Stopped` transition PATCHes exactly once, and the block is
  complete (a partial block would drop sibling keys, so assert all keys survive).
- `observed_at` alone does not trigger a write; a status change does.
- A stopped deployment stays in `/model/info` and appears in `/endpoints` as `offline`.
- A stopped deployment is excluded from routing.
- A deployment absent from OICM is deleted.
- A deployment that stops and then starts again is routable with a refreshed `api_base`.

## Open items

- A mass transition means N writes and N reloads in a burst, since there is no bulk endpoint.
  Accepted, because mass transitions are rare and a reload does not disturb in-flight
  inference (verified under eight-way concurrent load, zero errors). The alternative is
  pulling from the controller at serve time instead of pushing, which trades the write churn
  for a runtime dependency on the controller.
- The dev and prod gateways each carry eight dead rows in `LiteLLM_ProxyModelTable` whose
  `litellm_params.model` is an undecryptable blob, causing eight failing upserts on every
  reload. This predates this work and is tracked separately.
