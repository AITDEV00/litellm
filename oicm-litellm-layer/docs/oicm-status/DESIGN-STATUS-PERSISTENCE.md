# Model status persistence in LiteLLM `model_info`

Date: 2026-10-06
Status: design agreed and being implemented. Steps 1-9 of the implementation order
(the controller-side facts, transport, poll, and the existence rule) are implemented and
committed. Steps 10-12 (persisting the block, gating the write, and staleness) are the
current work.
Scope: how the controller stores OICM deployment status on the LiteLLM model row, how a
stopped deployment stays visible while becoming unroutable, and how a consumer tells fresh
status from a dead controller

For what has landed so far and the exact next step, see `PROGRESS-AND-PAUSED-WORK.md`. Note
that the OICM join key was never actually the blocker it was once recorded as; see the
correction in that file.

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
| `gateway_uuid` | the bare uuid, without any source prefix (see the join below) |
| `replicas: {desired, available}` | explains `Deploying` and degraded without a second lookup |
| `status_changed_at` | "stopped for N minutes", and the serving-flip-within-Ready case |
| `observed_at` | when the controller last heard from this deployment's source |
| `error_msg` | explains a `Failed`; stored in full, uncapped |

Dropped: `workspace_id` (constant, implied by controller config), `source_updated_at`
(documented as unreliable), `unavailable_replicas` (derivable), `workload_id` (already
present as `oicm_uuid`).

The cluster is deliberately **not** in this block. It lives in the sibling key
`oicm_cluster`, described next.

`api_base` is deliberately **not** in the block. An earlier draft persisted it, on the theory
that a stopped deployment cannot be re-probed. That was wrong: `litellm_params.api_base`
already survives on the row independently, and routing reads only that. Duplicating it into
`model_info` would create a second copy that can drift from the one the router uses. If it is
wanted for debugging, it belongs under a distinct key (for example `last_api_base`) and is
informational only.

`error_msg` is **not** capped. OICM's messages are short, and a truncated diagnostic is worse
than a long one. A generous guard (around 2000 chars) is worth adding only if a genuinely long
message is ever observed.

Existing sibling keys `oicm_uuid`, `oicm_namespace`, `oicm_source` stay as they are, and
`oicm_cluster` joins them.

### The join, and why the block carries `gateway_uuid`

The gateway groups rows by `oicm_uuid`. A Submariner import namespaces that value
(`submariner:abudhabi:<uuid>`, written by `SubmarinerImportSource`), while the owning OICM
returns the bare `deployment_id` (`<uuid>`). Grouping on the namespaced value and keying
snapshots on the bare one means a cross-cluster row can never find its status.

Two facts fix this together, and both are needed:

- `oicm_cluster` records which cluster the deployment is in, as a first-class field on the
  row.
- `gateway_uuid` records the bare uuid inside the status block, so a consumer can match a
  snapshot to a row without knowing which source named it which way.

### The source prefix is transport detail, not identity

`submariner:<cluster>:` exists because a k8s EndpointSlice needs the value to be unique, not
because it means anything about the deployment. So the prefix is stripped at both ends and
the cluster it encodes is preserved explicitly:

- `OicmModel.deployment_id` returns `strip_source_prefix(self.uuid)`, so the import and its
  own OICM's snapshot land on one key.
- `list_all_models_by_key` strips the same prefix when grouping.

Stripping on only one side is a real bug, not a theoretical one: `discovered` would be keyed
`submariner:abudhabi:<uuid>` while `litellm_by_key` would be keyed `<uuid>`, so the import
would match neither its own row nor its snapshot and would be registered a second time on
every cycle. Both sides call one shared helper (`strip_source_prefix`) so they cannot drift.

The cluster is not lost by stripping. `OicmModel.cluster` carries it (the in-cluster name for
a local deployment, the source cluster for an import) and it is persisted as `oicm_cluster`
on every row, written both at registration and on each status write so a row that predates
the field gains it.

Why not simply keep the prefixed uuid in the block: it would encode the cluster in a string
whose shape a consumer has to parse, and the value is genuinely different between the two
sides. A separate field says the same thing without either problem. `oicm_source` is left
exactly as it is: rewriting a stored value would change a row's provenance for no gain.

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
- **The status poller is the writer, on its own 10s clock.** It already holds the snapshots
  and the clock, so the write rides the poll rather than the 300s config reconcile. One
  PATCH carries the whole change: `{"blocked": <bool>, "model_info": {"oicm": {...}}}`.
  `blocked` is a top-level column and `model_info` is a nested object, and the endpoint
  accepts both in one body, so a status change is one write, not two.
- **`api_base` is refreshed by the k8s watch while the deployment exists**, so a restart
  transition refreshes it before the row becomes routable again.

### One writer, two clocks

Two writers touch a row, and they own disjoint keys, so they never fight:

| Writer | Clock | Owns |
|---|---|---|
| Status poller | 10s | `model_info.oicm`, `blocked` |
| Config reconcile (watch + full sync) | 300s | `litellm_params`, `model_info.mode` |

`blocked` has exactly one owner. Before this change the k8s watch set it from
`ready_replicas > 0` while the status poll would set it from OICM `serving_available`, which
is two opinions about one column. They agree today, but they are two probes of the same fact
and only one can be authoritative. **OICM `serving_available` is the owner.** The watch keeps
registering and refreshing `api_base` and still pauses a deployment it can see going down, as
a fast path for the same fact rather than a second source of truth. Reconciling `blocked` from
the watch as well is the part that goes away.

## Staleness, and how to detect a dead controller

An earlier draft put `observed_at` on every model and defined `stale` as
`now - observed_at > STATUS_STALE_AFTER`, refreshed by a per-model heartbeat. That is weak in
two ways, and the second is fatal to the stated goal.

1. It cannot tell "the controller died ten minutes ago" from "the controller died last
   week". Staleness only fires when the last write was recent, so a controller that stops
   writing is indistinguishable from one that died long ago.
2. A per-model heartbeat is one full reload per model per tick, for a timestamp that is
   identical across every model of the same source.

The facts being conflated are three, and separating them fixes both problems:

| Fact | Meaning | Where it lives |
|---|---|---|
| `observed_at` | when this deployment's status was last seen | per model, in `model_info.oicm` |
| `checked_at` | when this source was last polled at all | per cluster, in the heartbeat row |
| `stale` | we have lost contact with the source | derived at read time, never persisted |

`oicm_cluster` is what maps a model to its source's heartbeat, so a consumer reads
`oicm_cluster` off the row and then that cluster's `checked_at`.

Liveness belongs to the **cluster**, not the model. The controller polls both OICMs on one
timer, so one `checked_at` per source says everything: recent means the controller is alive
and both clusters are being watched; old means it is gone and every model from that source is
`unknown`. That is one write per cluster per tick instead of one per model, roughly a 25x
reduction, and it makes the answer sharper rather than weaker because it is per source: an Abu
Dhabi outage marks only Abu Dhabi unknown while Al Ain stays online.

### Where the heartbeat row lives

A model row is the wrong home: a per-source timestamp stored on a model duplicates across
that source's models, and hiding a sentinel row from model listings is not something the
controller can do (LiteLLM hides a model only when *all* of its deployments are `blocked`, and
an admin could flip that). LiteLLM has no generic KV write endpoint either.

So the heartbeat is a dedicated, controller-owned row carrying `oicm_heartbeat: <source>` and
no `oicm_uuid`. Because it has no `oicm_uuid` it is invisible to `list_all_models_by_key` and
therefore to every rule in this design, which is what makes it inert. It is `blocked = true`
so it is never routable, and it is a passthrough-shaped entry (a `hosted_vllm` model pointed at
an unreachable local base), never selected because it is blocked. Its cost is one write per
source per heartbeat, on the heartbeat cadence rather than the poll cadence.

This is a deliberate, small wart: one extra row per source in `/model/info` and the Admin UI
model list. The alternative, reusing `LiteLLM_Config`, needs a new LiteLLM endpoint and turns
on a reload fan-out per write, which is strictly worse. The row can be swapped for a proper
endpoint later without touching any consumer, because the reader only ever asks "what is the
latest `checked_at` for source X".

### What a consumer does with it

At read time, for a model whose `oicm.cluster` is `C`:

```
stale = now - heartbeat[C].checked_at > STATUS_STALE_AFTER
```

`stale` overrides the persisted status: a model whose last known status was `Ready` but whose
source has not been heard from within the window reports `availability = unknown`, not
`online`. That is the honest answer, and it is exactly the case the user asked for: a status
that has quietly gone out of date is worse than one marked unknown.
### The honest limit

Nothing can detect a controller that dies and is never replaced, because there is no writer
left to say so. A consumer can only ever see "last heard at T", and the rule above is the
strongest statement available: past the window, report `unknown` rather than `online`. The
heartbeat makes that judgement accurate and cheap; it cannot make it omniscient.

### Cadence

`STATUS_STALE_AFTER = 90s`, so three poll intervals (10s each) fit inside the window and a
single dropped or slow cycle does not flap a healthy source to unknown. The heartbeat writes on
its own cadence of `STATUS_STALE_AFTER / 3` = 30s, not on the 10s poll, so the extra writes are
one per source per 30s rather than one per source per 10s.

## The pre-existing guard this depends on

`compute_plan` used to append a patch for every matched key with no comparison against the
existing entry, so all 25 models were PATCHed every 300s whether or not anything changed. That
was 7,200 gateway writes a day of pure churn. The guard is in (`13c021c355`) and is a
prerequisite for the status writes being affordable, since every gateway write reloads every
model on the pod.

## Implementation order

Status of each item, as of 2026-10-06: 1 and 2 done, 3-6 the current work.

1. **Done** (`13c021c355`). Idempotence guard in `compute_plan` (compare the computed patch
   to the existing entry, skip when equal). Independent, safe, removes the existing churn.
2. **Done** (`792c09ee60`). Existence keyed on OICM `deployment_summary`: keep a deployment
   that is present in OICM even when the watch cannot see it, and delete a row only when it
   is absent from both. The `if not model.is_ready: continue` gate this step once named was
   removed in `8ee755efe2`, so there is nothing left to bypass. The rule is scoped to rows
   carrying `oicm_source == "local"`, so imports and admin rows are never deleted.
3. **Current.** `patch_status` writing the whole `model_info.oicm` block in the same PATCH as
   `blocked`, called only when a fact differs, excluding `observed_at`.
4. **Current.** Controller ownership of `blocked`, scoped to rows carrying `model_info.oicm`,
   with OICM `serving_available` as the sole authority. The watch stops reconciling `blocked`.
5. **Current.** `observed_at` per model plus a per-source heartbeat row carrying `checked_at`,
   with `STATUS_STALE_AFTER = 90s` and the heartbeat at `STATUS_STALE_AFTER / 3`.
6. Parallelize `LocalDeploymentSource.discover()`, which currently awaits each deployment
   serially (24 deployments in about 2.6s), so a faster cadence is affordable.

Not in this list but done alongside it: the controller now reads both clusters' OICM as
separate declared sources (`deploy/oicm/sources.yaml`), and `serving_available` no longer
requires `status_detail[].metadata`, which Abu Dhabi's OICM `1.7.1` does not populate.

Steps 1 and 5 are what make a shorter poll interval safe rather than harmful.

## Testing

- Idempotence: a second identical cycle issues zero PATCHes.
- Status change: a `Ready` to `Stopped` transition PATCHes exactly once, and the block is
  complete (a partial block would drop sibling keys, so assert all keys survive).
- `observed_at` alone does not trigger a status write; a status change does.
- A stopped deployment stays in `/model/info` and appears in `/endpoints` as `offline`.
- A stopped deployment is excluded from routing.
- A deployment absent from OICM is deleted, but only when the owning source was polled
  successfully in that cycle: an unreachable OICM must never read as deletion.
- A deployment that stops and then starts again is routable with a refreshed `api_base`.
- A cross-cluster row (`submariner:abudhabi:<uuid>`) joins to its Abu Dhabi snapshot on the
  bare uuid and gets its status and `blocked` set.
- A stale heartbeat marks a source's models unknown even when their last status was `Ready`.

## Open items

- A mass transition means N writes and N reloads in a burst, since there is no bulk endpoint.
  Accepted, because mass transitions are rare and a reload does not disturb in-flight
  inference (verified live: 26 metadata PATCHes against 6 concurrent real completions, zero
  failures, flat latency). The alternative is pulling from the controller at serve time
  instead of pushing, which trades the write churn for a runtime dependency on the
  controller.
- The heartbeat row is a small, visible wart (one extra row per source). See "Where the
  heartbeat row lives".
- The dev and prod gateways each carry eight dead rows in `LiteLLM_ProxyModelTable` whose
  `litellm_params.model` is an undecryptable blob, causing eight failing upserts on every
  reload. This predates this work and is tracked separately.
