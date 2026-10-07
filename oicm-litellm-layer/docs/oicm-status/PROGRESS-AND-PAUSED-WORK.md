# Status persistence: progress log and paused work

Date: 2026-10-06
Companion to `DESIGN-STATUS-PERSISTENCE.md` (the agreed design) and
`IMPLEMENTATION-CHECKLIST.md` (the step order).

For the complete record of the health-table change specifically (every file,
route contract, data model, verification, and open threads), see
`CHANGE-REPORT-2026-10-06-health-table.md`. It is the self-contained handoff
document for that work.

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

Steps 3-5 of the design order (persisting the block, `blocked` ownership, and
staleness) are now implemented on top of that: the status poller writes the
`model_info.oicm` block and `blocked` in one PATCH on its own 10s clock, OICM
`serving_available` is the sole owner of `blocked`, and per-model health plus
per-source liveness ride the native `LiteLLM_HealthCheckTable` through the new
`/oicm/v1/status-reports` and `/oicm/v1/heartbeats` routes (deployed to dev
2026-10-06), so a consumer can tell fresh status from a dead controller. See
"The status writer" and "Staleness: a per-source heartbeat" below.

The LiteLLM half of M1 (Steps 13-18) then landed in `2c7cf3bb64`: the official
OpenRouter `/endpoints` contract, `OpenRouterEndpointsMapper`, `GatewayStateResolver`
for `gateway_status`, freshness at request time, and the auth/visibility rules,
all verified live on dev. M3's rolling statistics (Step 25) shipped on top of
that (`15e02f87cf`, `9b6218a2bb`, plus the slice refactor), so the payload now
carries per-deployment latency, throughput, and uptime from real 30-minute
Prometheus windows. See `docs/openrouter/` for the mapping and logic map.

## Roadmap position

The plan is `IMPLEMENTATION-CHECKLIST.md` (25 steps, three milestones) plus the
six-step implementation order in `DESIGN-STATUS-PERSISTENCE.md`.

Milestone 1's **controller half is complete**. The controller fetches OICM
status in one call every 10s, derives `serving_available`, remembers
transitions, serves `GET /status`, and uses OICM as the source of existence so a
Stopped deployment stays registered and paused instead of disappearing.

| Step | Scope | Status |
|---|---|---|
| Design 1 | Idempotence guard in `compute_plan` | Done, measured on dev (`13c021c355`) |
| Design 2 | Existence from OICM `deployment_summary` | Done (`792c09ee60`) |
| 1 | Facts DTO | Done, reshaped as the `controller/status/` package |
| 2 | `OicmModel` carries workspace/workload/run ids | Superseded, not done |
| 3 | OICM client + token lifecycle | Done as `status/oicm.py::OicmStatusSource`, now one source per cluster |
| 4 | Secrets + config | Done, now declarative per source (`deploy/oicm/sources.yaml`) |
| 5 | Snapshot builder | Done |
| 6 | One workspace call | Done |
| 7 | 10s status poll | Done |
| 8 | `GET /status` | Done |
| 9 | Transition memory | Done |
| 10 | Persist `model_info.oicm` | Done, written by the status poller on the 10s clock |
| 11 | Write-amplification guard | Done for both writers (config diff in `compute_plan`, fact diff in the status writer) |
| 12 | OICM failure = staleness | Done, via native health-table rows (`oicm-source-*`) + `STATUS_STALE_AFTER` |
| 13-18 | LiteLLM `/endpoints` schema, mapper, `gateway_status`, dev validation | Done (`2c7cf3bb64`), deployed to dev |
| 19-23 | M2 engine-load telemetry | Not started (no `RuntimeTelemetryProvider` in the tree) |
| 24 | M3 omit what you can't provide | Superseded by Step 25 |
| 25 | M3 rolling stats from Prometheus | Done (`15e02f87cf` + `9b6218a2bb` + the slice refactor) |

Steps 1, 2, 11, 13, and 14 in the checklist describe an earlier shape than what
landed and have been annotated there. `controller/oicm_status.py` (a compat shim
for the `status/` package move) was deleted; nothing imported it.

## Current problems

No blockers. The LiteLLM half of M1 (Steps 13-18) is done and deployed, and M3's
rolling statistics (Step 25) shipped with it, so the `/endpoints` payload now
carries live latency, throughput, and uptime per deployment. The remaining work
is M2 (Steps 19-23), the instantaneous engine view from each runtime's own
`/metrics`, which is genuinely not started.

## Open work register (verified against the tree, 2026-10-07)

Every item still open, with its status and what it blocks. Grouped by where the
change lands. "Verified absent" means a code search for the symbol or behavior
came back empty, not that a doc said so.

### LiteLLM: genuinely unimplemented

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | M2 engine-load telemetry (Steps 19-23) | Not started | No `RuntimeTelemetryProvider` / `SGLangTelemetryProvider` / `VllmTelemetryProvider` / `RuntimeTelemetrySnapshot` / `kv_cache_utilization` anywhere in the tree. This is the *instantaneous* view (running requests, queued requests, KV-cache utilization) scraped from each runtime's own `/metrics`, distinct from the windowed statistics Step 25 serves. Needs per-runtime scrapers, a normalizer, a single-flight cache, and wiring into `gateway_status`. |
| 2 | Reserved `-1` endpoint status | Blocked on #1 | Deliberately unassigned in `gateway_status.py`. Meant to signal "idle then took a burst", which needs the engine-load telemetry of #1. Not independent work. |
| 3 | `perf_last_30m_by_workload` | Not started | Referenced only in a docstring. We have the data to fill the `text_generation` bucket, but classifying a metric series by workload (`text_generation`/`stt`/`tts`) needs a mapping that does not exist. Left absent rather than fabricated. |

### LiteLLM: correctness gaps on the `/endpoints` route

All four are documented as findings in
`docs/openrouter/MAPPING-litellm-to-endpoints.md` §8 and are live-confirmed.

| # | Item | Impact | Fix shape |
|---|---|---|---|
| 4 | Runtime detection reports every deployment as `openai-compatible` (§8a) | The vLLM/SGLang adapters never run: `provider_name`/`tag` are wrong, `/openapi.json` is never probed, `/model_info` is never probed, `api_capabilities` stays empty. The only workaround is a per-deployment `model_info.discovery_runtime` override. | `_detect_runtime_kind` matches only the literal `"sglang"`/`"vllm"`, but `provider` is `litellm_params.custom_llm_provider`, which for a DB-registered model is `hosted_vllm`. Teach the matcher `hosted_vllm`/`hosted_sglang`. Small, but changes what every endpoint advertises, so it deserves its own change. |
| 5 | Capability enrichment result is discarded (§8b) | Dead work on every request: `get_model_endpoints` calls both enrichers, but `map_endpoints` never reads `capabilities`. | Either consume it (feed `supported_parameters` / modality fields) or stop calling it on this route. |
| 6 | `max_prompt_tokens` / `max_completion_tokens` always `null` (§8c) | Structurally empty, not per-deployment values. Verified: only `openai_compatible.py:78` constructs `ModelLimits`, and it sets `context_length` alone. | Populate the limit fields from the probe or the registry, or accept the nulls. |
| 7 | `data.architecture` hardcoded empty (§8d) | The real architecture (`model_type`, `architectures`, `tokenizer`, `instruct_type`) exists in `model.architecture` for SGLang deployments, and modalities in `model.capabilities`, but this route emits neither. The list route does map modalities. | Read `model.architecture` in `_response_architecture()`. |

### LiteLLM: semantic decision to confirm

| # | Item | Notes |
|---|---|---|
| 10 | `live_concurrency` sums across replicas | The query is `sum by (model_id) (litellm_deployment_in_progress_requests)`. One `model_id` is one deployment, but a deployment can have several pods, so the value is the fleet-wide in-flight count for that deployment. The alternative is per-replica max ("peak load on the busiest pod"). `MAPPING-usage-metrics.md` §8f discusses this max-vs-sum tension for the historical surfaces, but the field itself does not state which it is. Pin it down and document it, since a reader will assume one or the other. Not a bug, a contract clarification. |

### Controller

| # | Item | Notes |
|---|---|---|
| 8 | `LocalDeploymentSource.discover()` is serial | Verified: it awaits `discover_for_deployment` inside a `for` loop, each doing a configmap read plus two HTTP probes. 24 deployments take about 2.6s. Parallelizing with `asyncio.gather` is a pure latency win. This is Step 6 of the design order. |

### Deployment

| # | Item | Notes |
|---|---|---|
| 9 | Prod runs the pre-fix image | Prod pods started 2026-09-30 on tag `jya0-v1.102.0`; the registry tag was overwritten on 2026-10-07 with the new build, so prod pods are still the old layers. The gauge-leak fix, the telemetry fields, and everything documented as "live on dev" are dev-only. Prod still shows the phantom concurrency and has none of the telemetry fields. Explicitly deferred; recorded so it is not mistaken for done. |

### Smaller items

- The checklist's Step 1 says "create `controller/oicm_status.py`", which never
  happened (it became the `status/` package with `serving_available` replacing
  `is_ready`), and Step 2's three ids were never added. Both are superseded rather
  than missed: Step 6 dropped `workload_run_id` and `workload_status` as having no
  consumer, and `deployment_id == workload_id` is the uuid the reconciler already
  keys on.
- The old sentinel heartbeat model rows are gone (reconciler deletes them; nothing
  recreates them). Liveness is an `oicm-source-<cluster>` row in the native health
  table. See "Staleness: a per-source heartbeat".
- The old sentinel heartbeat model rows are gone (reconciler deletes them; nothing
  recreates them). Liveness is an `oicm-source-<cluster>` row in the native health
  table. See "Staleness: a per-source heartbeat".

## The status writer

Step 10 lands as `StatusPersister`, called by `StatusPoller` after each
`refresh()`. It is a separate class from the reconciler because it owns a
different clock (10s) and a disjoint set of keys.

### One PATCH, two owners

A status change writes `blocked` and the block together:

```
PATCH /model/{id}/update
{"blocked": true, "model_info": {"oicm": {...}}}
```

`blocked` is a top-level column and `model_info` is a nested object, and the
endpoint accepts both in one body, so a status change is one write, not two.
`model_info` merges shallowly, so sending the whole `oicm` object replaces it and
leaves the other ~140 keys alone.

### The fact set, and the one key excluded

The writer compares a fixed set of facts against what the row already carries:
`status`, `serving_available`, `replicas`, `status_changed_at`, `error_msg`,
`cluster`, `gateway_uuid`, and the routing flag `blocked`. `observed_at` is
deliberately excluded from the comparison, because including it would make every
cycle differ and therefore every cycle write. It is advanced only when some other
fact changed, so a steady-state cluster issues zero status writes.

### Why the poller writes rather than the reconciler

The poller already holds the snapshots and the 10s clock, so the write rides the
poll. Putting it in `compute_plan` instead would tie status latency to the 300s
resync and would entangle `observed_at` with the model-config diff.

### `blocked` has one owner

The k8s watch used to set `blocked` from `ready_replicas > 0` while the status
path set it from OICM `serving_available`: two probes of the same fact and two
opinions about one column. They agree today, but only one can be authoritative.
**OICM `serving_available` is the owner.** The watch still registers a model,
refreshes `api_base`, and pauses one it can see going down as a fast path for the
same fact; it no longer reconciles `blocked` against the k8s replica count.

### Verified live

26 metadata PATCHes against 6 concurrent real completions: zero failures, flat
latency (0.33/0.34/0.35s), and the block persisted with all 143 sibling
`model_info` keys intact. The reload a PATCH triggers does not create a serving
hole: `clear_cache` deliberately does not wipe ordinary DB deployments, only
auto-router ones.

The health-table path was verified live on dev (2026-10-06): 25 per-model rows and
2 `oicm-source-*` rows in `/health/latest`, `checked_by = oicm-controller`, OICM
truth (status word, serving_available, cluster) carried in `details`, writes only
on change/hourly refresh plus one heartbeat POST per source per 30s, and 401 for
unauthenticated callers. This surfaced and fixed a native bug on the way: the
generated Prisma client rejects plain dicts for `Json?` columns, so every
health-row write carrying `details` silently failed until `_clean_details` wrapped
its output in `prisma.Json`.

## Staleness: a per-source heartbeat

An earlier design put `observed_at` on every model and derived `stale` from it,
refreshed by a per-model heartbeat. That was weak: it cannot tell "the
controller died ten minutes ago" from "last week", because staleness only fires
when the last write was recent, and a per-model heartbeat is one full reload per
model per tick for a timestamp identical across a whole source.

Liveness belongs to the **source**, not the model. `checked_at` is written once
per source per heartbeat into a controller-owned row (tagged
`oicm_heartbeat: <source>`, no `oicm_uuid`, `blocked = true`). Having no
`oicm_uuid` is what makes it invisible to `list_all_models_by_key` and therefore
to every rule in this design.

A consumer computes `stale = now - checked_at > STATUS_STALE_AFTER` per source.
`STATUS_STALE_AFTER = 90s` (three poll intervals, so one slow cycle does not flap
a healthy source), and the heartbeat writes every `STATUS_STALE_AFTER / 3` = 30s,
not on the 10s poll.

`stale` overrides the persisted status: a model whose last known status was
`Ready` but whose source has not been heard from within the window reports
`unknown`, not `online`. The honest limit is that a controller which dies and is
never replaced cannot be detected by anything, since no writer is left; the
strongest available statement is "past the window, report unknown".

### The heartbeat row is a placeholder

The heartbeat is a controller-owned model row per cluster, carrying
`oicm_heartbeat` and `checked_at` with no `oicm_uuid`. It is the only place a
controller-owned timestamp can live today, because LiteLLM has no generic
key-value write endpoint, and the cost is one non-model row per cluster in admin
`/model/info`.

It is meant to be replaced. The real consumer of all of this work is
`/api/v1/endpoints`, the OpenRouter-convention surface a user queries for model
status, which is being built on the LiteLLM side. Once that surface can carry a
controller-owned status record, the heartbeat moves there and the rows go away.
Any consumer written now should ask for "the latest `checked_at` for cluster X"
rather than read the row directly, so the swap needs no consumer change.

## The join fix for cross-cluster rows

The gateway row for an Abu Dhabi deployment is keyed
`submariner:abudhabi:<uuid>` (written by `SubmarinerImportSource`), while Abu
Dhabi's own OICM returns the bare `<uuid>`. Grouping on the namespaced value and
keying snapshots on the bare one meant an AD row could never find its status.

### The prefix is transport detail, so it is stripped on both sides

`submariner:<cluster>:` exists so a k8s EndpointSlice value stays unique. It
means nothing about the deployment, so it is stripped everywhere and the cluster
it encodes is kept explicitly:

- `OicmModel.deployment_id` is now `strip_source_prefix(self.uuid)`.
- `list_all_models_by_key` strips the same prefix when grouping.
- Both call one shared helper, so they cannot drift apart.

This mattered more than the original status gap. An earlier revision stripped
only on the gateway side, which left `discovered` keyed
`submariner:abudhabi:<uuid>` while `litellm_by_key` was keyed `<uuid>`. The
import then matched neither its own row nor its snapshot and would have been
**registered a second time on every cycle**. Both sides now agree by
construction, and a regression test asserts it.

### The cluster is a first-class field

`OicmModel.cluster` holds the cluster a deployment physically runs in: the
in-cluster name for a local deployment and the source cluster for an import. It
is persisted as `oicm_cluster` on every row, written at registration and again on
each status write so a row that predates the field gains it. This is what
answers "Abu Dhabi or Al Ain" for a model without anyone parsing a uuid.

`oicm_source_name` is the second origin field, recording which configured OICM
reported the status. It is separate from `oicm_cluster` because the two are
different facts that only coincide today: if the gateway later runs in Abu Dhabi,
or a source is named for its role rather than its location, a local deployment
and an imported one could claim the same cluster while different OICMs reported
them. The heartbeat lookup and the delete rule key on the source, so that is what
they read.

Two guards came out of this and both are enforced:

- `CLUSTER_NAME` has no default. A guessed cluster is a wrong answer that looks
authoritative, so a missing value fails at startup. Both Deployments set it.
- Cluster names must be distinct across sources. An ambiguous cluster would make
the heartbeat lookup wrong rather than merely slow.

`oicm_source` is left untouched: rewriting a stored value would change a row's
provenance for no gain.

### A silent field-drop this caught

`OicmSourceConfig.with_env_overrides` rebuilds the config field by field, and the
new `cluster` field was not in that list. The dataclass default is `""`, so the
override path, which is the path the Deployment actually runs, would have written
an empty cluster on every row while the parse-only tests passed. Found by
exercising the override path rather than the parser, fixed, and pinned by a test
that fails if the field is dropped again.

## The deletion rule now

Existence is the union of the sources. A row is deleted only when the poll that
says so was complete: `StatusPoller.all_sources_ok` is false as soon as any
configured source fails, and the reconciler then skips every delete that cycle.
An unreachable OICM therefore never reads as deletion, which is the failure the
earlier `oicm_source == "local"` scope was written for but could not express
once a second cluster existed.

The scope is now "the row carries an `oicm_uuid`": that covers local rows and AD
imports while leaving admin rows and the heartbeat rows (which have no
`oicm_uuid`) alone.

## Tests

267 controller tests pass. The 2 failures in `test_config.py` are pre-existing
and unrelated: they read the prod manifest, whose master key is now
`sk-05132025`, and they fail identically with this work stashed. The count is
277 with the cluster-identity tests.

Nine mutations were each killed by the new tests:

- comparing `observed_at` in the write guard (defeats the guard)
- ignoring `blocked` in the guard (a routing flip would be missed)
- one heartbeat per model instead of per source
- removing the `allow_deletes` gate (a failed poll would delete a cluster)
- reverting the delete scope to `oicm_source == "local"`
- not stripping the `submariner:` prefix, at both the guard and the client level
- ignoring the heartbeat's own cadence
- removing the no-snapshot heartbeat guard (it must not consume the window)
- sending `blocked` and the block as separate writes
- `deployment_id` not stripping the prefix (the double-register bug)
- dropping `gateway_uuid` from the block
- ignoring `oicm_cluster` on the row, so a stale row would never be backfilled

## Multi-cluster status and the declarative source map

The controller now reads **both** OICM instances at once, and which instances
exist is data rather than code.

### Sources are declared, not hardcoded

`deploy/oicm/sources.yaml` is a ConfigMap listing one entry per OICM instance:
endpoints, realm, client, and workspace. `controller/sources_config.py` loads it
(`OICM_SOURCES_FILE`, default `/etc/oicm/sources.yaml`) and validates it. Adding a
cluster is an entry plus a Secret, with no code change.

The file serves both roles: applied as a ConfigMap in-cluster, and read directly
by a local run, which unwraps the ConfigMap envelope so there is one copy rather
than two that can drift.

### Credentials stay in Secrets

A ConfigMap cannot hold or interpolate a Secret value, and the controller's
ServiceAccount has no `secrets` permission, so credentials cannot live in the
source map. Each source instead names its credentials via variables derived from
its name (`OICM_SOURCE_ALAIN_USERNAME` / `_PASSWORD`), wired by the Deployment
from that source's own Secret with `secretKeyRef`. A source listed in the
ConfigMap whose credentials are absent is skipped with a warning, so a partial
rollout degrades to fewer sources instead of failing every cycle.

### One source class, not one per version

Both OICM versions (Al Ain `1.15.19`, Abu Dhabi `1.7.1`) share `OicmStatusSource`.
They differ only in the shape of `status_detail`, which the shared availability
logic handles, so a second class would duplicate the transport and token
lifecycle for nothing.

`StatusPoller` takes a list of sources and polls them concurrently, so a cycle
costs the slowest source rather than the sum. Each source's failure retains its
own previous snapshots and is logged; the other sources' results still land, so
an Abu Dhabi outage never makes Al Ain's deployments look deleted. `GET /status`
reports each snapshot's `source`, so a merged view stays attributable.

No uuid prefixing is needed: the two clusters' uuids are disjoint, and prefixing
would have rippled into the reconciler's keys and the gateway join.

### The Abu Dhabi `metadata` gap is closed

`status_detail[].metadata` is genuinely absent from OICM `1.7.1`: it is not in
that version's `StatusDetail`, `WorkloadStatusDetail`, or `DeploymentInstance`
schemas, while it is in Al Ain's. But the fact it carries is not lost. `status`
is populated identically in both versions, so `_is_ready` now treats `metadata`
as optional and falls back to the entry's own `status`:

- Pod: `metadata.ready`, or no metadata and `status == "Running"` (still
  requiring a node, since an unscheduled pod cannot serve)
- LeaderWorkerSet: `metadata.available`, or no metadata and `status` serving
- Deployment: still not consulted, because it can report available while a
  rollout is in progress

`metadata` still wins when present, so a version that reports readiness keeps its
more precise answer. Verified live: the Abu Dhabi deployment `766b1720`
(`zai-org/GLM-5.2-FP8`) previously read as not serving and now reads
`serving_available: true`.

Al Ain's `/health.is_ready` is `false` for a running Ready pod (Al Ain omits
`apiVersion`) while Abu Dhabi's is `true`, so `is_ready` is unreliable in opposite
directions across the two. That is why the design dropped it in favour of
`status_detail`, and why it must not be reintroduced.

### Verified live on dev

26 snapshots: 25 from `alain`, 1 from `abudhabi`, both tokens and both
`deployment_summary` calls returning 200. The single Abu Dhabi deployment reports
`source_status: Ready`, `serving_available: true`.

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
  the watch and OICM, and the delete rule runs only on rows carrying an
  `oicm_uuid` (and only when the owning source was polled successfully), so a
  Submariner import or an admin-added model is never collateral damage.
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

Verified: 219 controller tests pass (the 2 `test_config.py` failures are
pre-existing and read a live manifest whose key changed, unrelated to this
work). Eight mutations were each killed by the new tests: making the delete
scope unconditional, deleting an OICM-only deployment instead of pausing it,
registering OICM-only deployments, re-keying the gateway group on the composite,
reverting `_handle_delete` to deregister, making `deployment_id` the model name,
skipping a tracked uuid on re-add, and letting an OICM-only placeholder into
`new_state`.

Run them with `uv run --extra test --python 3.13 python -m pytest tests/controller`
from `oicm-litellm-layer/`. `pydantic` was added to `pyproject.toml`: the status
wire models import it and the image installs it, so the test extra was not
self-contained without it.

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

The scope was once `oicm_source == "local"`, which was written for one cluster
and could not express "the owning OICM was unreachable". It is now keyed on the
row carrying an `oicm_uuid` plus the owning source having been polled
successfully, so a second cluster fits without a special case.

| Row carries | Source polled OK | In its OICM | Action |
|---|---|---|---|
| `oicm_uuid` | yes | yes | keep, set status, pause or resume |
| `oicm_uuid` | yes | no | delete |
| `oicm_uuid` | no | unknown | leave alone; an unreachable OICM is never deletion |
| no `oicm_uuid` | n/a | n/a | leave alone, not controller-managed |

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

The controller half of M1 is complete, and the LiteLLM half (Steps 13-18) plus
M3's rolling statistics (Step 25) shipped on top of it. The full list of what
remains is the "Open work register" above. Ranked by value, the recommended
order is:

1. Item 4: runtime detection. It silently makes `provider_name`, `tag`, and the
   capability fields wrong for every deployment, so it is the highest-value fix.
2. Item 8: parallelize `LocalDeploymentSource.discover()`. Small and purely a
   latency win.
3. Items 5-7: the "fill the honest gaps" pass (discarded enrichment, the two
   limit fields, `data.architecture`).
4. Item 1: M2 (Steps 19-23), the large one. Item 2 depends on it.
5. Item 10: pin down the `live_concurrency` max-vs-sum semantics and document it.

Known honest gaps in the `/endpoints` payload, none of them telemetry:
`max_prompt_tokens` / `max_completion_tokens` are always null (only the
openai-compatible adapter constructs `ModelLimits`, and it sets `context_length`
alone), `data.architecture` is hardcoded empty, runtime detection reports every
deployment as `openai-compatible` so the vLLM/SGLang adapters never run, and the
capability enrichment result is discarded on this route. See
`docs/openrouter/MAPPING-litellm-to-endpoints.md` §8.

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

The cross-cluster import is a Submariner import, not an OICM-managed deployment,
but it is now first-class for status: the prefix-stripping join above lets its
gateway row find Abu Dhabi's snapshot, so it gets a real status and `blocked`
from OICM like any local row. Its existence still comes from the import, so the
deletion rule leaves it alone (a row with no `oicm_uuid` is not the controller's
to remove).

One difference that no longer matters: Abu Dhabi runs OICM `1.7.1` against Al
Ain's `1.15.19`, and `1.7.1` does not serve `/api/v1/workspaces/{ws}/deployments`
in the same shape, but the controller only uses `deployment_summary`, which both
serve identically. Both clusters' OpenAPI documents declare
`/v1/workspaces/{workspace_id}/deployment_summary`.

The RKE2 certificate fault that made Abu Dhabi's exports unreachable is fixed.
See `oicm-aa-ad-cluster-interconnect/abudhabi-rke2-cert-renewal-runbook.md`.

Full findings, evidence, and the reusable relay procedure are in
`oicm-aa-ad-cluster-interconnect/abudhabi-oicm-rest-api-export.md`.
