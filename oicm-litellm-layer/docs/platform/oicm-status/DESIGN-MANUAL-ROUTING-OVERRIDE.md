# Manual routing override for OICM deployments

Status: Proposed. This document describes a change to implement, not the current behavior.

Date recorded: 2026-11-17

## Problem

A proxy administrator may need to pause a controller-managed deployment while its OICM
workload is still running, for example to move callers to a compatible backend before
undeploying the original. Today that manual pause is undone automatically.

`blocked` represents two different decisions at once:

- Platform state: OICM reports whether the workload is serving.
- Operator intent: a proxy administrator has deliberately paused the route.

Those must be separate facts. Platform availability cannot remain the sole source of truth for
both facts if an administrator is expected to pause a healthy deployment deliberately.

## Reproduction observed on 2026-11-17

An administrator patched `blocked: true` on `Qwen/Qwen3-Next-80B-A3B-Instruct`, deployment id
`3c29b046-72c9-4169-8832-05d7616b9779`, while its OICM workload still reported
`serving_available: true`.

The controller replaced the flag with `blocked: false` on the next status write, even though the
stored status facts had not otherwise changed:

```text
10:32:37 Patched status on litellm_id=3c29b046-...: status=Ready        serving=True  blocked=False
10:33:33 Patched status on litellm_id=3c29b046-...: status=Undeploying  serving=True  blocked=False
10:34:03 Patched status on litellm_id=3c29b046-...: status=Stopped      serving=False blocked=True
```

The final block came from OICM becoming non-serving, not from the administrator's earlier request.

Two controller paths currently undo an admin pause:

- `controller/status_persister.py::plan_writes` computes `blocked = not snapshot.serving_available`.
  Its compare-and-write guard notices the manual `blocked: true` value differs from the computed
  value, so an otherwise unchanged row is rewritten. `test_blocked_alone_is_a_change` in
  `tests/controller/test_status_persister.py` pins this current behavior.
- `controller/reconciler.py::SyncReconciler.compute_plan` schedules an unblock whenever a desired
  serving model has a blocked row, including after a stop/start round trip. The new guard must also
  cover this path or the two writers will continue to disagree.

The status poll normally runs outside the slower full-sync reconciler, so the faster manual-block
reversal typically comes from the status writer.

## Requirements

- Preserve a manual pause while OICM continues to report the deployment as serving.
- Let the same endpoint also support a deliberate manual unblock without causing a controller
  revert on the next cycle. The data model should not silently make unblocking sticky when the
  workload later stops unless that is explicitly part of the approved contract.
- Restore automatic lifecycle reconciliation on explicit request, without requiring an OICM
  stop/start cycle to clear a manual pause.
- Continue automatic lifecycle behavior for every existing row that has never had an admin
  routing action: available stays routable and stopped stays paused.
- Keep recording observed OICM state, health, cluster, task and other metadata when an admin route
  override is active.
- Do not suppress new-ingress non-serving safety. A newly registered non-serving deployment remains
  blocked until an authorized operator overrides it.
- Do not infer controller writes from a human user id or the current proxy user: automated and human
  calls can use the same proxy-admin identity.
- Do not persist observed writes to the override object itself or piggyback administrator intent on
  `oicm.serving_available`: those remain machine observations and operator policy respectively.
- Apply the same policy to public block/unblock endpoints and the generic PATCH endpoint
  (`model_name`/`litellm_params`/`blocked`).

## Contract and state

### Operator state

Add a dedicated operator-intent object in the existing `model_info` JSON, for example:

```json
{
  "routing_override": {
    "v": 1,
    "state": "auto",
    "manual_blocked": false,
    "updated_by": "admin-user-id",
    "updated_at": "RFC3339 timestamp",
    "reason": "Migrate callers to the Flash backend before undeploy"
  }
}
```

`routing_override` is not an OICM-owned identity key. It is intentionally preserved on subsequent
controller status patches as operator state. Omitting `oicm_uuid` continues to mean the deployment
is unmanaged, which is a different concern from pausing a managed deployment.

`state` supports two mutually exclusive modes:

- `auto`: ignore `manual_blocked`; effective route blocking is the inverse of platform serving.
- `manual`: effective route blocking equals `manual_blocked`.

Removed or unrecognized override state is treated as `auto`. Proxy-controlled parsing must reject
invalid objects instead. Existing rows have no override and automatically behave as they do today.

A narrower boolean override is smaller but makes an intentional unblocked state indistinguishable
from legacy data once the platform stops serving. The explicit mode and explicit unblock command
together avoid silently freezing a deployment in the routable state forever after it dies. Implement
this explicit reason + actor + time object with a strict schema so unrelated unknown JSON cannot
silently suppress reconciliation. The route API remains simple for clients, while stored intent
records who changed it for auditing.

### Effects on routing

| Platform serving | Override state | Effective `blocked` | Who owns it next |
|---|---|---|---|
| true | absent or `auto` | false | Controller |
| false | absent or `auto` | true | Controller |
| true | `manual`, `manual_blocked: true` | true | Operator |
| false | `manual`, `manual_blocked: true` | true | Operator |
| true | `manual`, `manual_blocked: false` | false | Operator |
| false | `manual`, `manual_blocked: false` | false | Explicit operator override |

`model_info.oicm.serving_available` remains the observed platform fact even if effective routing
disagrees with it. The Admin UI should show both rather than implying a healthy serving deployment
is unhealthy just because an operator paused it.

Before writing `auto`, the reader uses the stored override not the last write it produced, so rows
are durable across controller restarts, multiple controller replicas and gateway reloads. No DB
schema migration or override ConfigMap is needed for the default JSON shape.

## Implementation

### 1. Gateway: carry operator intent in both admin write paths

In `litellm/proxy/management_endpoints/model_management_endpoints.py` factor the route write into
two operations:

- `set_manual_block(model_id, blocked, actor, reason)` sets override state `manual`, applies the
  desired boolean to the effective route flag and stores audit fields. It also handles missing
  `routing_override` safely and rejects invalid override data with a 4xx validation error.
- `restore_automatic_routing(model_id, actor)` sets override state `auto` and computes effective
  blocking only when observed OICM evidence exists. It preserves paused rows when observed data is
  absent, stale, inaccessible or ambiguous. The controller later clears a stale block on the first
  successful OICM serving observation.

Both need an atomic DB read-modify-write transaction to avoid losing a concurrent actor's object
fields, plus the existing model audit log and router cache reload behavior in the usual one-call
path. Audit events must record transition to/from automatic mode as well as the boolean result.

Wire the existing `POST /model/block`, `POST /model/unblock` and `PATCH /model/{model_id}/update`
`blocked` request paths through this helper, including appropriate authorization. Add an explicit
admin-only `/model/{model_id}/route/auto` command so a one-way UI pause does not leave a row manual
forever with no supported way back. If the UI only uses generic `blocked`, extend its request
contract with an explicit `routing_mode` or equivalent to express restore-auto; do not make an
ordinary `blocked: false` PATCH silently do it. This API choice needs confirmation during design
review. Until restored explicitly, override intent is durable across stop/start cycles, including a
manual unblock on a stopped backend, and the lifecycle exposes the manual badge so it cannot be
misread as normal service availability.

For read compatibility keep the existing measured `blocked` field and effective-route behavior:
router selection still reads a single `blocked` column. `routing_override` is control-plane metadata.
It does not change request dispatch or the health-table fields and it does not require Prisma schema
changes because the deployment `ModelInfo` allows arbitrary metadata. As is customary for nested
model_info, unmarked fields are preserved by merge update rather than replaced wholesale.

Care is required at registration: a newly registered non-serving row must get the appropriate pause
without manufacturing manual intent. The status writer should set effective routing for such a row,
not the new override helper.

### 2. Controller: persist observation independently from policy

Add one pure decision helper, located next to the existing routing-state semantics in the controller
rather than in two different modules. It takes the stored `model_info` and snapshot availability and
returns whether the effective route should be paused. `plan_writes` and `compute_plan` both call it.

For `plan_writes`, read the override separately from `model_info.oicm`. Retain the original facts
comparison (`v`, `status`, `serving_available`, `replicas`, `status_changed_at`, `error_msg`,
`gateway_uuid`): absence of a lifecycle difference is still valuable. Compare the effective desired
route against stored route using the override decision, so an active manual override no longer
triggers a revert merely because OICM reports serving. An override transition itself triggers a new
computed route PATCH; ordinary health or cluster/task backfills continue as today.

For `SyncReconciler.compute_plan`, replace both serving unblocks and non-serving pause paths with
that same decision helper. Keep the per-key richest-entry selection, alias deduplication, file model
update and UUID presence semantics untouched.

During a cross-source import reconciliation preserve `gateway_uuid` keying and apply the imported
workload's stored override to the imported deployment, not to an unrelated unmanaged row with the
same public model name.

A simplified controller-read implementation that honors `manual` on reads is valid without querying
a further data source. The gateway holds override writes and the controller preserves stored values.
The controller never invents an override or clears it on lifecycle change.

### 3. Expose routes in the Admin UI

Show OICM availability and effective route separately. For example:

```text
Platform: Ready, serving
Route: Paused by operator
Reason: Migrate callers to Flash before undeploy
Changed by: admin-user-id at timestamp
```

An explicit "Restore automatic routing" action calls the new auto endpoint. "Pause route" and
"Resume route" call manual block/unblock respectively. A paused badge is never used as a substitute
for serving availability because the two facts are independent.

If the current UI is built from `LiteLLM_ProxyModelTable` and generic PATCH, it can keep its shape
for effective routing and fetch `routing_override` out of `model_info` without schema migrations.
Update dashboard OpenAPI types from the backend generator rather than committing hand edits to the
generated schema file alone. Existing UI consumption of `blocked` alone cannot set a durable
manual override without this marker, so a previous click must not masquerade as one when upgrading.

## Lifecycle requirements

- Normal startup and stop are unchanged for rows in auto mode.
- Manual block on a healthy row survives at least a 10-second status poll and a 300-second full
  reconcile, with no deletion or registration churn.
- Manual unblocking while serving likewise remains unblocked when later statuses observe serving.
- A manual pause survives OICM stop/start until the administrator explicitly restores automatic
  routing.
- Removing the OICM workload does not create a duplicate unmanaged row and its status and delete
  rules remain unchanged.
- Once OICM no longer lists the workload, its managed row is still removed regardless of override.
  Operator state must never turn deletion into retention of a nonexistent backend.
- An unavailable or stale OICM source does not authorize an auto-unblock, a restore-auto to resume a
  route, or an override-created delete.
- An unknown override is read-only corruption data needing explicit repair; no automatic disabling or
  unblocking is inferred from it.
- Effective paused routing is honored across public routers, health filtering and direct deployment
  ID routes as before.

## False alternatives

**Rely on `updated_by` to distinguish people from the controller.** Human admins and automated
writes can share identities. Proxy roles alone cannot determine an act's purpose and are not a
portable override representation. A controller-specific service principal can be introduced later to
improve auditing but does not replace explicit intent.

**Preserve every historical `blocked: true` forever.** This would prevent manual reverts but break
standard automatic stop/start recovery. Once a stopped deployment restarted, it would remain paused
unless a human remembers the old pause. A separately stored manual decision is necessary.

**Use the existing `oicm` status block for manual intent.** That block is compared against the fresh
observation and logically owned by OICM. Mixing operator intent into it invalidates both ownership
and the status comparison.

**Use config or `model_alias_map` for this.** These are different planes addressed to unrelated
settings and identities. A pause applies to the concrete OICM-managed deployment UUID behind the
`oicm_uuid`; an aliased public label is not a suitable independent override key.

## Minimal implementation order

1. Add the gateway override storage helper and wire both block forms into it, including a supported
   auto-restore action and audit transitions. Unit test race-safe merging and existing key survival.
2. Add the shared controller effective-route helper and use it from `plan_writes` and `compute_plan`.
   Migrate the old one-owner unit tests to the new table semantics rather than just changing
   asserted outputs.
3. Expand Admin UI display into platform observation plus operator policy with an explicit restore
   action. Cover PATCH, block/unblock, JSON model info and OpenAPI interaction parity.
4. Add test coverage for routing behind blocked rows and lifecycle absence, then mutation tests for
   every effective-block branch, override preservation, auto clearing and write suppression.
5. Build dev first with `make controller-build-push TAG=<tag>` and
   `make controller-deploy-dev BUILD=0 TAG=<tag>`, and deploy the gateway image using its dev
   target. Test the complete flow through the real admin UI and real curl against live OICM rows.
   Promote the same validated image with `BUILD=0` to prod only after dev passes. Do not silently
   alter live behavior while reviewing this document.

## Regression test matrix

At minimum add focused tests to `tests/controller/test_status_persister.py`, the existing
`tests/controller/test_reconciler.py` and the mapped gateway model-management tests. Tests are
meaningful only if a revert without intent fails on the old release, an override survives both paths,
and missing lifecycle status never resumes traffic automatically.

- `test_manual_block_serving_not_reverted_by_unchanged_status`: serving status + effective blocked
  manual row -> `plan_writes == []`. This directly replaces the old unconditional-revert scenario.
- `test_manual_block_serving_kept_by_config_reconcile`: managed serving row -> no unblock or delete.
- `test_manual_block_survives_stop_start_and_controller_restart`: intent read from the row and kept
  across an OICM round trip.
- `test_manual_unblock_serving_does_not_add_writes_when_route_is_already_open`: does not race with
  next status write.
- `test_manual_unblock_stopped_persists_until_explicit_auto`: explicitly acknowledged persistent
  operator override, with degraded UI serving visibility, not a hidden permanent exception.
- `test_restore_auto_resumes_only_with_fresh_serving_snapshot`: stale/missing status keeps pause and
  healthy observed serving resumes. Restore removes the override and returns to today's lifecycle.
- `test_auto_lifecycle_regression`: stopped halts; restarted resumes through both controller paths;
  deleted unmanaged/workload rule does not change.
- `test_alias_view_only_shares_effective_flag`: repeated deployment ID from an alias is not a second
  override instance and cannot be deleted as a duplicate.
- `test_status_metadata_changes_still_persist_under_override`: health and cluster/task updates still
  occur without mutating the override or clearing the effective manual block.
- `test_new_non_serving_registration_gets_safety_block_without_manual_override`: safety behavior from
  watch registration remains owned by status, not promoted into operator state.
- `test_public_api_patch_block_and_unblock_have_override_semantics`: both supported admin entrypoints
  persist identical intent and preserve audit trails.
- `test_invalid_override_fails_validation_never_resumes`: strict shape and enum validation fail closed
  to the observed platform route, not to an unsafe auto-unblock.
- `test_manual_override_survives_concurrent_gateway_and_controller_writes`: JSON fields and stale
  controller PATCH behavior preserve each other's owned fields.

## Use case this solves

A serving OICM backend can stay live for rollback while an administrator blocks just its gateway
route and registers a no-thinking alternative under the same public model name during migration.
After testing the replacement, the operator undeploys the original; an original Stopped state still
blocks it until explicit restoration, while an OICM full deletion cleans its row as today. No
permanent exclusion, duplicate UUID or controller restart is required for the route pause itself.
