# Status persistence: progress log and paused work

Date: 2026-10-06
Companion to `DESIGN-STATUS-PERSISTENCE.md` (the agreed design) and
`IMPLEMENTATION-CHECKLIST.md` (the step order).

This file exists so nothing is lost while the work is paused. It records what
landed, what is verified, what is blocked, and the exact next step.

## Where the work is

Step 1 of the implementation order (the `compute_plan` idempotence guard) is
done, committed, deployed to dev, and measured. Step 2 (a non-serving
deployment stays registered and is paused) is done and committed (`8ee755efe2`),
and the rest of step 2 is now implemented: the join is keyed on `oicm_uuid`, the
delete rule is scoped to controller-managed rows, and OICM's
`deployment_summary` is wired into existence. See "Step 2: the uuid join and
OICM existence" below.

## Correction: the join was never broken

The "blocker" recorded below was a misdiagnosis and is left in place only as a
record of the wrong turn. The composite key `{oicm_uuid}::{model_name}` compares
the **gateway's** `model_name`, which is the served id the controller itself
registered, against the **discovered** served id from `/v1/models`. Those always
agree, so the join worked all along. The "12 mismatches" table compared OICM's
`model_name` (the GUI label) against the gateway, which is not what the code
ever did: the reconciler never reads OICM's `model_name`.

The real gap was smaller and is now closed: a Stopped deployment has no k8s
Deployment and no Service, so the watch could not see it and the reconciler read
its absence as deletion. Existence now comes from OICM.

## Step 2: the uuid join and OICM existence

`OicmModel.composite_key` (`{uuid}::{model_name}`) is replaced by
`OicmModel.deployment_id`, which is the uuid alone. One deployment is one model,
so the uuid is the identity, and it is the one key OICM and the gateway reliably
share. The composite was introduced for a multi-model deployment that does not
exist (24 k8s Deployments yield 24 discovered models), and keeping it was the
only thing that made the join look fragile.

- `list_all_models_by_key` groups by `oicm_uuid` alone.
- `LocalDeploymentSource.discover_for_deployment` keys by uuid and registers the
  first served id. A deployment advertising several ids is truncated with a
  warning, because a second row sharing the uuid could never be matched back.
- `compute_plan` takes an optional `oicm_models` map. Existence is the union of
  the watch and OICM, and the delete rule runs only on rows whose
  `oicm_source == "local"`, so a Submariner import or an admin-added model is
  never collateral damage.
- A deployment known only to OICM (a Stopped one) keeps its registered row and
  is paused. It is not registered from scratch: only the watch yields a served
  model id and a resolving `api_base`, so OICM alone would create a
  permanently-blocked row with nothing behind it.
- `full_sync` refreshes the status poller and passes the snapshots in. An OICM
  failure falls back to watch-only and is never read as deletion.
- `_handle_delete` now pauses instead of removing. A k8s deletion is either a
  stop or a real delete, and only OICM can tell them apart, so the next full
  sync removes the row only if OICM no longer lists it.

The controller watches every 300s and the status poll runs every 10s, so the
stale window between a k8s delete and the OICM-driven removal is bounded by the
resync interval.

Verified: 218 controller tests pass (the 2 `test_config.py` failures are
pre-existing and read a live manifest whose key changed, unrelated to this
work). Six mutations were each killed by the new tests: making the delete scope
unconditional, deleting an OICM-only deployment instead of pausing it,
registering OICM-only deployments, re-keying the gateway group on the composite,
reverting `_handle_delete` to deregister, and making `deployment_id` the model
name.

## Landed and verified (earlier)

### Step 1: idempotence guard (`13c021c355`)

`compute_plan` now compares a patch against the stored entry and skips it when
every key it would write already holds that value. Previously it patched all 25
models every 300s regardless, which is 7200 gateway writes a day of pure churn.

Measured on dev: 25 patches per cycle before, 0 on a clean cycle after, 1 when a
real change exists (proved by injecting a wrong `api_base` and watching the next
cycle restore it).

The guard is what makes a shorter poll interval affordable, since every gateway
write reloads every model on the pod and fans out to the other replicas.

### Step 2, k8s-visible half (`8ee755efe2`)

Committed and verified live on dev:

- `OicmModel.serving` models the lifecycle (False while the deployment exists but
  is not serving).
- `build_oicm_model` builds a record for a deployment known only from OICM, which
  is what lets a Stopped deployment stay registered.
- `compute_plan` keeps a non-serving model registered and emits `blocks` instead
  of a delete, and resumes a model that came back.
- `_handle_add` registers a not-ready deployment and pauses it instead of
  dropping it.
- `_handle_modify` pauses on the way down and resumes on the way up, so routing
  follows the deployment without a re-register.
- `LiteLLMClient.set_blocked` writes the `blocked` column.

Verified live on dev: `blocked=true` returns 200, reads back as `True`, keeps the
model id stable, and a call to that model returns **403** while other models
still return 200. Unblocking restores 200. So the pause genuinely removes a model
from routing without deleting it.

Tests: 212 pass. Five mutations, each killed: dropping a non-serving model
instead of blocking it, never resuming a restarted deployment, blocking
unconditionally (churn), `_handle_add` ignoring not-ready, and `_handle_modify`
never pausing or resuming.

## The blocker that turned out not to be one: the OICM join key

This section is kept as a record of a wrong turn. The "blocker" was a
misdiagnosis: the composite key compares the gateway's own `model_name` against
the discovered served id, which always agree, so the join was never broken. The
table below compares OICM's `model_name` (a GUI label) against the gateway, which
the code never did. The real gap was that a Stopped deployment has no k8s object,
and that is now closed by sourcing existence from OICM. See the correction near
the top.

The original analysis, for the record. Step 2's remaining half needs to match an
OICM deployment to its registered gateway model. The only join available is
`{oicm_uuid}::{model_name}`, and it was believed not to work. Measured on dev
against all 25 deployments:

```
Ready deployments: model_name matches gateway: 12 | mismatches: 12

MISMATCH 9dcd9568 | oicm: RadixArk/GLM-5.3-NVFP4        | gateway: zai-org/GLM-5.3
MISMATCH 894cea22 | oicm: BIS: Qwen/Qwen3.6-35B-A3B-FP8 | gateway: Qwen/Qwen3.6-35B-A3B-FP8
MISMATCH a67da10a | oicm: None                          | gateway: hamsa-tts-new
MISMATCH 87bc52a5 | oicm: None                          | gateway: PP-DocLayoutV3
MISMATCH 9aff17c0 | oicm: None                          | gateway: inception-tts
```

Three distinct causes:

1. Renamed in OICM: `RadixArk/GLM-5.3-NVFP4` became `zai-org/GLM-5.3`, and a
   separate deployment already carries `zai-org/GLM-5.3`, so two deployments now
   collide on the gateway name.
2. A display-name prefix: `BIS: Qwen/Qwen3.6-35B-A3B-FP8` in OICM versus the bare
   id in the gateway.
3. OICM carries no `model_name` at all for the native-surface deployments
   (`hamsa`, `inception`, `PP-DocLayout`).

**Root cause, confirmed by the platform owner:** the `model_name` field in OICM is
the **deployment name as shown in the OICM GUI**, not the model id the model
server actually serves. The served id is set per deployment by a separate env var
(`MODEL_NAME` or `MODEL_ID`) and is what discovery reads from `/v1/models`.

So `model_name` is a human-facing label and must never be used as an identity.
Keying existence on it would mis-handle 12 of 24 models, and the failure would be
silent: a non-matching OICM entry would look like a new deployment and re-register
a duplicate, while the real entry would look absent from OICM and get deleted.

### The join that does work

Join on `oicm_uuid` alone. Verified across all 25 deployments:

```
gateway uuids : 25
oicm uuids    : 25
in both       : 24
gateway only  : 1  <- submariner:abudhabi (zai-org/GLM-5.2-FP8, cross-cluster import)
oicm only     : 1  <- 93457b11 (Stopped, currently not registered)
```

Supporting checks:

- The composite key is not load-bearing today: 24 k8s Deployments yield 24
  discovered models, and zero gateway uuids host more than one `model_name`.
- `LiteLLM_ProxyModelTable.model_id` is a `String @id @default(uuid())` with no
  inbound foreign keys, so nothing references a row's id.
- The affinity pin is self-healing when an id disappears:
  `_find_deployment_by_model_id` returns None, logs `pinned deployment=... not
  found in healthy_deployments`, and falls back to normal routing.

### A third category the design doc does not cover

The gateway carries a cross-cluster import with `oicm_source: submariner:abudhabi`
and `api_base: http://242.0.0.253:8080/v1`. It has no OICM record because it is an
import, not a local deployment. So the OICM-absence rule must be scoped to
`oicm_source == "local"`, or it would delete every Submariner import.

Two entries (`hamsa-stt`, `hamsa-tts`) have `oicm_source: None` and no
`oicm_uuid`, so they survive by accident rather than by design. Worth an explicit
decision.

### Scope table for the existence rule

| `oicm_source` | In OICM | Action |
|---|---|---|
| `local` | yes | keep, set status, pause or resume |
| `local` | no | delete |
| `submariner:*` | n/a | leave alone, not OICM-managed |
| absent | n/a | leave alone, not controller-managed |

## Why a redeploy changing the uuid is correct

Confirmed as intended behavior, not a problem:

- The uuid is OICM's record identity and `oip/workload-id` mirrors it, so delete
  and recreate legitimately produces a new identity.
- Preserving the gateway row would be wrong, not merely unnecessary: `api_base`
  points at `s-<uuid>.adeo.svc`, and a redeploy creates a Service named for the
  new uuid, so a preserved row would point at a Service that no longer exists.
- Nothing references the row's id (no inbound FKs), and the affinity pin
  self-heals with one lost sticky turn.
- The gap is well inside the 300s resync, and the model is legitimately offline
  during it. The one cosmetic difference is that the model is briefly absent
  (404) rather than present-and-paused (403).

## Next step

The uuid join and the OICM existence wiring are done (see "Step 2: the uuid join
and OICM existence" near the top). The remaining steps of the implementation
order are:

1. Step 10: `patch_model_info` writing the whole `model_info.oicm` block, called
   only when the block differs, excluding `observed_at`.
2. Step 11: the write-amplification guard for the status block, independent of
   the model-config diff in `compute_plan`.
3. Step 4: controller ownership of `blocked`, scoped to rows carrying
   `model_info.oicm`, with the reason recorded. Note `blocked` is currently set
   directly by `compute_plan` and the watch handlers, so this is a narrowing, not
   new machinery.
4. Step 5: the `observed_at` heartbeat on a cadence derived from
   `STATUS_STALE_AFTER`.
5. Step 6: parallelize `LocalDeploymentSource.discover()`, which still awaits
   each deployment serially.

## Still open for the OICM team

Now that the join is on the uuid, the served model id is no longer needed to
match a deployment. It is still worth asking whether OICM can expose it, because
it would let the controller label a Stopped row correctly instead of leaving the
registered name as-is:

- Is there a field carrying the model id the server actually serves, the one set
  by the per-deployment `MODEL_NAME`/`MODEL_ID` env var?
- Is `registered_model_name` or `model_version_name` populated for any deployment?
  Both were `None` on every deployment sampled on 2026-10-06.
- Is the `BIS:` prefix on some `model_name` values a display convention?

## Related: the Abu Dhabi side

The cross-cluster import (`oicm_source: submariner:abudhabi`) is not OICM-managed
today, so it is out of scope for the existence rule. Making Abu Dhabi's own OICM
status usable the same way Al Ain's is turns out to need two fixes, neither of
which is in this repository's code:

- Abu Dhabi runs OICM `1.7.1` against Al Ain's `1.15.19`, and `1.7.1` does not
  serve `/api/v1/workspaces/...`, so the controller's status source would need a
  version adapter or an OICM upgrade.
- Newly exported Abu Dhabi services are unreachable because kube-proxy there is
  failing every watch with `Unauthorized` (expired RKE2 internal certs).

Full findings, evidence, and the reusable relay procedure are in
`oicm-aa-ad-cluster-interconnect/abudhabi-oicm-rest-api-export.md`.
