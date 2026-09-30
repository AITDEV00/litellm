# Routing-Strategy Sync-Task Leak -> 12Gi OOMKills — Logic Map

> **Technique**: [Logic Mapping](../techniques/logic_mapping_technique.md) —
> trace-through-before-you-build. Phase 1 (Trace) + Phase 2 (Test) complete;
> live-verified against prod AND dev (`adeo-litellm`, 2026-09-30). Fix deployed
> to dev and verified; prod rollout pending.
>
> Companion docs: `analytics-reads-prisma-engine-oom-LOGIC-MAP.md` (the
> *first* OOM cause, fixed 2026-09-15, commit `229a288d39`),
> `retention-flags-and-oom-LOGIC-MAP-v2.md` (the 09-08 DB-incident OOM). This
> map covers the **third and final OOM driver** — a Python-side asyncio task
> leak, distinct from both prior incidents. If pods still OOM after this fix,
> the remaining suspect is the bounded-but-large heavy-query prisma engines
> (2 x 1.17GB/pod), which are a floor-raiser, not a ratchet.

---

## 0. One-paragraph answer

Every config/model reconcile (firing ~every 19s in this deployment, driven by
DB-stored models + the discovery controller + Redis pubsub config sync)
rebuilds the routing-strategy selector because `Router.update_settings`
treated the *presence* of the `routing_strategy_args` key as a change — and
the DB overlay row always carries that key, even when its value is the same
empty `{}` as before. Each rebuild constructs a new
`LowestTPMLoggingHandler_v2` (usage-based-routing-v2), whose
`BaseRoutingStrategy.__init__` spawns an immortal `while True` Redis-sync
task at a **0.1s interval** — and nothing ever cancelled the replaced
selector's task. Workers stranded ~3.2 such tasks per minute (measured
live), pinning their strategy object, `DualCache`, increment queue, and a
never-reset, ever-growing key set. After 3.5–4.5h a worker held 822–1,126
tasks and 1.2–3.8GB RSS — while `gc_objects` stayed flat, because the
retained memory is large `str`/`bytes` (bypassing pymalloc, invisible to
`len(gc.get_objects())`) churning the glibc brk heap — and the pod hit its
12Gi limit. The 09-24 investigation that concluded "architectural, no leak"
was wrong: it read a 1-minute rotated log window with 4 workers' samples
interleaved, which erased the trend.

## 1. The kill chain

```text
DB-stored models + discovery controller watch + UI edits
    |
    v
Redis pub/sub: litellm_proxy.config_change (min-resync 10s, debounce 1s)
    |                                     config_sync_pubsub.py
    v
ProxyConfig._resync_config_from_db -> add_deployment() (proxy_server.py:7490)
    |
    v
_add_router_settings_from_db_config -> llm_router.update_settings(**overlay)
    |                                                        (proxy_server.py:6934)
    |   overlay ALWAYS contains "routing_strategy_args" (LiteLLM_Config row,
    |   verified via psql: {"routing_strategy_args": {}, ...} even when empty)
    v
Router.update_settings: routing_args_updated = True   [BUG: no change check]
    |
    v
_apply_updated_routing_strategy_args() -> _build_strategy_selector()
    |                                       (router.py:1272, 1335)
    |   constructs NEW LowestTPMLoggingHandler_v2 (v2 strategy)
    v
BaseRoutingStrategy.__init__ -> setup_sync_task()
    |   loop.create_task(periodic_sync_in_memory_spend_with_redis, 0.1s)
    v
NEW IMMORTAL TASK BORN. The old selector is only
_unregister_router_selectors()'d from the callback lists — its task is
NEVER cancelled (cleanup() existed with zero callers).
    |
    v
Each stranded task forever runs at 10Hz:
    - pins strategy object + DualCache + increment queue
    - in_memory_keys_to_update NEVER reset (comment said "max 1000 keys",
      code was plain .add(); v2 rpm/tpm keys embed %H-%M minute timestamps
      -> unbounded cardinality), so each tick MGETs the whole history
    - large str/bytes allocations (>=512B bypass pymalloc -> glibc malloc)
      fragment the brk heap; glibc never returns freed arena memory
    - holds Redis pool sockets open: 179 CLOSE_WAIT to 10.42.2.42:6379 on one
      pod; fat worker held 228 socket fds vs 31 on a healthy worker
    |
    v
Worker RSS: 430MB fresh -> 3.8GB in ~4.7h (measured +81..+186 MB/MIN on the
runaway worker in the pre-OOM logs, gc_objects DECREASING at the same time)
    |
    v
4 workers x 1.2-3.8GB + engines + master > 12Gi cgroup -> OOMKill
    (both pods killed 2026-09-30: wfqd9 at 06:52 after 68 min, ck76d 09:13)
```

## 2. Where the numbers come from (live evidence)

| Evidence | Value | Source |
|---|---|---|
| Sync-task census, prod workers | 822 / 847 / 975 / 1126 per worker (4.7h uptime) | `/debug/asyncio-tasks` (fork endpoint), 16-sample burst to separate the 4 granian workers |
| Fresh-pod baseline | exactly 4 tasks/worker (prod), 1 (dev, 1 worker) | same, post-restart |
| Leak rate | +16 tasks / 5 min = **3.2/min/worker** (1 per ~19s) | live 5-min measurement on ck76d |
| Dev pod before fix | **4995 tasks / 23056 total**, 2d8h uptime, 942Mi | `/debug/asyncio-tasks` on dev |
| RSS growth on runaway worker | +91.5, +93, +136.5, +186, +81 MB per 60s sample, while gc_objects fell 1.37M -> 1.26M | previous-container `mem_monitor` logs (wfqd9 OOMKilled run) |
| Heap composition | fat worker: 2675MB in glibc `[heap]` (brk) + 887MB arenas; healthy: 504MB brk + 352MB | per-mapping smaps census (`/proc/<pid>/smaps` script) |
| CLOSE_WAIT sockets | 179, all to Redis 10.42.2.42:6379 | `/proc/net/tcp` census |
| DB overlay content | `{"routing_strategy_args": {}, "routing_strategy": "usage-based-routing-v2", ...}` | psql on `LiteLLM_Config` |
| Local repro | 200 RouterBudgetLimiting instantiations -> 200 tasks; 50 v2 selector rebuilds -> 50 tasks; cleanup() -> 0 | `.venv` one-off scripts |

## 3. The exact code sections at fault (all UPSTREAM code)

| Defect | Location | Introduced by (upstream) |
|---|---|---|
| Immortal `while True` sync task, `should_batch_redis_writes=True` spawns it | `router_strategy/base_routing_strategy.py:39` `setup_sync_task` | Krish Dholakia, 2025-04-30, PR #10458 |
| v2 selector uses **0.1s** sync interval | `router_strategy/lowest_tpm_rpm_v2.py:58` | same era |
| Unconditional `routing_args_updated = True` (the trigger) | `router.py` `update_settings`, `routing_strategy_args` branch | Clement, 2026-09-10, PR #40352 (TTFT routing) |
| `cleanup()` exists but had zero callers | `base_routing_strategy.py:64` | — |
| Never-reset `in_memory_keys_to_update` ("max size of 1000" comment, plain `.add()`) | `base_routing_strategy.py:29,154,195` | PR #10458 era |
| Second leak twin: `RouterBudgetLimiting.__init__` spawns `asyncio.create_task()` with NO reference kept (un-cancellable in principle) | `router_strategy/budget_limiter.py:112` | Ishaan Jaff, 2024-12-13, PR #7220 |
| Reconcile driver (how often update_settings fires) | `proxy/common_utils/config_sync_pubsub.py` | mateo-berri, 2026-07-31 |

The fork's contribution was **circumstance, not code**: DB-stored models
(`store_model_in_db: true`) + the discovery controller + pubsub config sync
make reconciles fire every ~19s, vs the rare manual config edits of a typical
upstream deployment. Same bug, firehose instead of drip.

## 4. Why it is NOT the earlier hypotheses (and why the 09-24 investigation was wrong)

| Hypothesis | Disproof |
|---|---|
| "Stable plateau, architecture problem, no leak" (09-24) | Both pods OOMKilled the same day, 3.5h after restart. The "plateau" was a 1-minute rotated log window with 4 workers' samples interleaved — the trend was erased by reading it wrong. Live census: tasks grow monotonically 3.2/min/worker. |
| "gc_objects is flat, so no leak" | `gc_objects` counts GC-tracked objects; large `str`/`bytes` (>=512B) bypass pymalloc, go to glibc malloc, and are invisible to `len(gc.get_objects())`. smaps census shows the growth IS there: +2.1GB in the brk heap. The runaway worker gained +186MB/min while gc_objects *fell*. |
| "~3GB master + non-Python overhead" (09-24's unexplained gap) | Per-process census: the gap was the prisma engines (6 engines, two late-spawned heavy-client engines at 1.17GB each) — separate, bounded issue from the 09-15 map. |
| Spend-log queue unbounded | Row-capped `deque(maxlen=SPEND_LOG_QUEUE_MAX_ROWS=50000)`; drains verified. |
| Stream tracer (`LITELLM_STREAM_TRACE_PATH`) | File-backed, rotation+retention capped (42MB), writes healthy. |
| Prometheus cardinality | Series grow slowly; label sets bounded (44 user agents). |
| Loguru `enqueue=True` | `SimpleQueue`, OS-pipe bounded (64KB). |
| Redis response cache | 630K keys but 367MB/512MB with `allkeys-lru` — bounded; not pod memory. |

## 5. The fix (commit `20483ef59d`, 2026-09-30, deployed to dev)

Four defects, one cancellation choke point plus one trigger guard:

1. **Trigger guard** (`router.py` `update_settings`):
   `routing_args_updated = value != self.routing_strategy_args` — the DB
   overlay echoing the same args is now a true no-op (mirrors the existing
   `routing_strategy` equality check ten lines above). Kills ~99% of
   rebuilds.
2. **`dispose()` on `BaseRoutingStrategy`**: cancels `_sync_task` without
   awaiting (CancelledError is a BaseException, escapes the loop's
   `except Exception`); nulls the reference. `cleanup()` now also nulls it.
3. **Disposal wired at the single choke point**:
   `_unregister_router_selectors` (which every selector-discard path already
   flows through — strategy init, args rebuild, routing-group rebuild)
   disposes every passed selector. Plus
   `_remove_optional_callbacks_of_type` disposes in its global-cleanup
   branch, and `RouterBudgetLimiting` now keeps `self._sync_task` so its
   task is cancellable at all.
4. **Key-set hygiene**: `_sync_in_memory_spend_with_redis` differences out
   the synced keys at the end of a successful pass (concurrent adds and
   failed passes survive; per-minute keys no longer accumulate forever);
   hard cap `ROUTING_STRATEGY_IN_MEMORY_KEYS_MAX=10000` (env-overridable).

Design note: reset-upfront (get_and_reset at the top of the sync) was
considered and rejected — the empty-increment-queue tick early-returns
before the Redis fetch, which would silently drop pending keys. The
difference-out-at-end shape is the correct one.

Regression tests: `tests/test_litellm/test_router_strategy_sync_task_leak.py`
(6 tests: identity no-op, repeated-reconcile task count, changed-args
rebuild disposes old task, strategy switch disposes, budget-limiter dispose,
budget-limiter removal disposes) + extended
`tests/test_litellm/router_strategy/test_base_routing_strategy.py` (key-set
reset semantics, cap, dispose idempotency, cleanup). Mutation-tested both
directions: reverting the guard -> identity test fails; reverting all
router.py edits -> 5 tests fail.

## 6. Live verification (dev, 2026-09-30, image `jya0-v1.102.0`)

| Metric | Old dev pod (2d8h, buggy image) | New pod (fixed image) |
|---|---|---|
| Leaked sync tasks | **4995** | **1** |
| Total asyncio tasks | 23,056 | 10 |
| Census over 6 min | growing ~2.7/min | flat `[1 x 13]` under live reconcile traffic |
| mem_monitor RSS | 942Mi climbing | 529.6MB, `delta_mb=+0.0` |
| "Routing strategy:" rebuild log lines | (constant churn) | zero while 56 router/sync log lines fired |

Real chat completion through the dev gateway: 200 (Kimi-K3). Fix markers
(`def dispose`, the guard line) verified present in the deployed venv.
Prod expectation after rollout: 4 legit tasks/worker steady (4 strategies
x 4 workers... 4 workers x 1 strategy = 4 per worker is wrong; correct: one
per strategy per worker — census showed 4/worker on prod's fresh pod),
workers staying at the ~430MB fresh baseline.

## 7. Upstream convergence — READ BEFORE THE v1.104.x MERGE

Upstream independently found and fixed this exact bug on **2026-09-24**:
commit `fc87a06f00` — *"fix(proxy): stop leaking periodic tasks on every DB
config reload (#42784)"* (devin-ai-integration[bot], co-authored
yassin@berri.ai). It first ships in `v1.104.0-dev.2` / `v1.104.0-rc.1`, so
it is NOT in our `v1.102.0` base — both fixes will meet at the next merge.

Merge resolution plan (per-file):

- **`update_settings` guard**: their line is byte-identical to ours
  (`routing_args_updated = value != self.routing_strategy_args`) —
  auto-merges. Zero work.
- **`base_routing_strategy.py`**: theirs introduces `cancel_sync_task()` +
  `retire()` (retire = cancel + flush pending increment queue to Redis
  before dying — a nice touch ours lacks; the leaked selector's queued
  spend counters get written instead of dropped). Ours introduces
  `dispose()` (cancel + null the ref) + the key-set difference-out + cap,
  which theirs lacks entirely (their key set still never resets).
  **Resolution: adopt their `retire()` mechanism as the single primitive,
  keep our key-set hygiene verbatim.** Their `retire()` does not null the
  task reference — fold our null-ref into it.
- **`router.py` `_unregister_router_selectors`**: both added a disposal
  loop at the same choke point; theirs uses
  `isinstance(selector, BaseRoutingStrategy)` (stricter, typed — preferred
  per repo typing discipline), ours used `getattr(selector, "dispose",
  None)`. **Resolution: take theirs, extend it to also dispose
  `RouterBudgetLimiting` instances**, which their fix misses completely —
  without our extension, the budget-limiter variant of this leak returns
  for anyone using `router_budget_limiting`.
- **`router.py` `_remove_optional_callbacks_of_type`**: ours only; keep.
- **Tests**: keep both suites; theirs adds routing-group rebuild coverage
  (`test_router_routing_groups.py` +105 lines) and a SlackAlerting task
  leak fix we never touched — take the Slack fix as part of the merge.

Also noted: their `#42784` fixed a *separate* `SlackAlerting` periodic-task
leak in the same commit — that one we never had symptoms for, but it comes
free with the merge.

## 8. Detection playbook (how to never be fooled by this class again)

- `/debug/asyncio-tasks` census is the ground truth for task leaks; sample
  each worker (round-robin) and compare over minutes, not one snapshot
  (the endpoint load-balances across granian workers).
- `gc_objects` flat + RSS growing = large-buffer leak (str/bytes outside
  pymalloc); do per-mapping `smaps` census to confirm brk-heap growth.
- Container log rotation can erase the evidence window — pull `--previous`
  container logs after an OOMKill immediately.
- The mem_monitor growth attribution (tracemalloc window) never fired
  because no single 60s delta crossed the 200MB threshold while the
  runaway grew +81..+186MB/min in ~1MB/chunk increments. Thresholds tuned
  for spikes miss steady drips; the task census catches them.
