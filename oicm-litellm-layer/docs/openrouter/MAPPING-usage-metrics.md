# Mapping: litellm usage data to latency/throughput endpoint fields

Answers the question: what usage/telemetry does litellm natively capture,
how accurate is it, and what could populate `latency_last_30m`,
`throughput_last_30m`, `uptime_last_5m/30m/1d`, and the reserved `-1`
load-based `status`.

Live verification: prod `https://litellm.ecouncil.ae`, 2026-10-07
14:41-15:15 UTC, key `sk-05132025` (from `docs/credentials.md`). All
findings below were observed on the actual deployment, not inferred.

Companion doc: `MAPPING-litellm-to-endpoints.md` (the field-by-field
`/endpoints` mapping; its "needs telemetry" rows are the consumers here).

## 1. What litellm natively captures (the inventory)

Three tiers, all populated on every routed request:

| Tier | Store | Fields | Granularity | Retention |
|---|---|---|---|---|
| raw request log | `LiteLLM_SpendLogs` | `startTime`, `endTime`, `request_duration_ms` (int ms), `completionStartTime` (TTFT end; forced to `endTime` for non-streaming), `completion_tokens`, `cache_hit`, `status`, `model_group`, `custom_llm_provider`, entity columns (team/user/end_user/api_key/agent_id) | per request | janitor (60d floor) |
| 1-minute rollup | `LiteLLM_ModelPerformanceRollup` | per model_group per minute: `request_count`, `completion_tokens`, `throughput_tokens_sum`, `ttft_seconds_sum`, 32-bin ttft histogram, `starts`/`ends` counters | per minute | same janitor |
| Prometheus | `/metrics` | `litellm_deployment_in_progress_requests` (gauge), `litellm_deployment_total_requests_total` (counter), `litellm_output_tokens_metric_total` (counter), `litellm_llm_api_time_to_first_token_metric` (histogram), `litellm_deployment_latency_per_output_token` (histogram) | 30s scrape | 15d |

The rollup's `(starts, ends)` counters exist specifically so peak
concurrency can be recomputed at read time as a running sum (write path in
`model-performance-optimization-LOGIC-MAP.md`, read path in
`model-performance-30d-read-fix-LOGIC-MAP.md`).

Two read surfaces:

| Endpoint | Source | Concurrency definition | Throughput definition |
|---|---|---|---|
| `GET /model/metrics/per_model` (realtime page) | Prometheus, per deployment (model_id) | `max by (model_id) (max_over_time(gauge[window]))` | `sum by (model_id) (rate(output_tokens[window]))` |
| `GET /model/performance?window=5m/15m/1h` | Prometheus, merged to model group | `sum by (model_id, litellm_model_name) (max_over_time(gauge[step]))` | same counter rate, `sum by (model_id, requested_model)` |
| `GET /model/performance?window=24h/7d/30d/...` (global) | rollup table | running sum of starts-ends, max per bucket (peak) | per-request `completion_tokens/request_duration_ms*1000`, averaged per bucket then across buckets |
| `/model/performance` entity-scoped or custom range | raw SpendLogs SQL | same running-sum peak | same per-request average |
| `/spend/logs`, `/spend/logs/ui` | raw SpendLogs | per-request `request_duration_ms`, TTFT | — |

## 2. The user's suspicion, verified on prod

Claim checked: "realtime is not accurate, especially concurrency and
throughput versus historical". Verdict: **confirmed for concurrency, and
the two surfaces disagree with each other; throughput is directionally
right but defined differently per surface.**

### 2.1 Realtime concurrency is a leaked gauge, not real load

Observed on prod (DB ground truth computed from the same window):

| Deployment | realtime gauge (per pod) | `/model/performance` 1h | DB truth |
|---|---|---|---|
| hamsa/hamsa-stt | 690 and 644 | **1334** flat | 0 in flight; zero traffic since 2026-10-05 08:47 UTC; rollup starts=ends=677 over lifetime |
| zai-org/GLM-5.3-Flash | 418 and 335 | 418+335 summed into group | DB running-sum peak last 1h = 9 |
| zai-org/GLM-5.3 | 362 and 353 | ~713 | peak last 1h = 5 |
| Qwen/Qwen3.8-Flash-Next-FP8 | 21 and 22 | merged | peak last 1h = 21 (this one looked real at 14:44 but drained to 0 by 15:05; the residual was transient overlap, not a leak) |

Mechanism, traced in the code and confirmed by metric accounting on the
pods:

- The gauge is OICM-custom (`deployment_in_flight.py`). The ledger incs in
  `async_pre_call_deployment_hook` (fires inside the `@client` wrapper,
  before the provider call) and decs in
  `set_llm_deployment_success_metrics` / `set_llm_deployment_failure_metrics`.
- On both prod pods, hamsa-stt has **only** the gauge series: zero
  `deployment_total_requests`, zero success/failure counters. 644 incs
  fired whose terminal logging event never ran. The STT path
  (`atranscription` via router `_atranscription` -> `litellm.atranscription`,
  also `@client`-wrapped) enters the inc, but its failures can terminate
  without a prometheus `async_log_failure_event` (e.g. exceptions raised
  after the wrapper's hook but before logging setup, client aborts, or
  failures in pre-call checks) so the dec never lands.
- For chat models the same leak exists but smaller: GLM-5.3-Flash pod
  79vdh counters show 71,359 total / 71,165 success / 194 failure since
  pod start, yet the gauge sits at 418 while DB peak concurrency is 9.
  The ratchet is event-correlated, not steady: the gauge climbed from ~50
  to 442 during a 12:21-12:36 UTC window with 239 failed requests
  (ReadTimeouts / 4xx burst), then froze. Failures whose logging path
  skips the prometheus dec leave a permanent +1 each; retries re-enter the
  wrapped function and fire another inc while the failed attempt's dec
  never ran.
- The Aug 24 fix (commit `0b0413b4f2`) aligned model_id resolution between
  the inc and the success dec; it is deployed in prod. The remaining leak
  is inc-without-any-dec (terminal events that never reach prometheus at
  all), which that fix cannot address.

### 2.2 The two realtime surfaces contradict each other

For the same minute, same prod:

- `/model/metrics/per_model` hamsa-stt concurrent = **690** (max over pods)
- `/model/performance` 1h hamsa-stt concurrent = **1334** (sum across pods:
  `_merge_series_into` sums every series sharing the model name)

Both are wrong (truth is 0), and they disagree by construction. Even with a
healthy gauge the two are different quantities: max-across-replicas (peak
load on the busiest pod) vs sum-across-replicas (fleet-wide concurrency).
The endpoints page must pick one definition; today it has two.

### 2.3 Historical (DB/rollup) concurrency is the accurate one

The rollup/DB running-sum matches ground truth: recomputing the running
sum directly over SpendLogs for the last hour gives peaks of 9 (GLM-5.3-
Flash), 24 (Kimi-K3), 34 (Qwen3-Embedding-4B), and the 24h endpoint's
rollup numbers (GLM-5.3-Flash avg_concurrent 16.4, GLM-5.3 2.68) are
consistent with the per-minute starts/ends counters. Known approximations,
both benign:

- 1-minute granularity (a sub-minute burst can peak higher than the bucket
  max reports).
- Boundary-time approximation: concurrency is derived from request
  start/end timestamps, not true in-flight instants. A request whose
  logged end is delayed (flush queue) shifts its -1 late, inflating the
  tail of its minute. With 30s flushes and minute buckets this is noise.
- `endTime` is never NULL in prod (18.9M rows checked), so there is no
  missing-end distortion today.

### 2.4 Throughput definitions differ per surface (all "true", none wrong)

Same prod hour, three views of GLM-5.3-Flash:

- realtime per-model: `rate(litellm_output_tokens_metric_total[1h])` last
  = 1126.9 tok/s (fleet instantaneous output rate)
- `/model/performance` 1h: same counter aggregated, avg 1248 tok/s
- 24h rollup: avg_throughput = 101.1 (per-request tokens/sec, averaged)

The realtime number is "how many tokens per second is the fleet producing
right now"; the DB number is "what was the average per-request generation
speed". A customer reading `throughput_last_30m` expects the OpenRouter
definition (tokens/sec of generation speed per request, i.e. the DB
definition). The counter-rate number also includes time when nobody is
streaming, which drags it toward zero; it is a capacity metric, not a
per-request speed metric.

### 2.5 TTFT is consistent where it exists

Prometheus TTFT (`litellm_llm_api_time_to_first_token_metric`) and DB TTFT
(`completionStartTime - startTime`) both exclude nothing/everything
identically only for streaming; DB TTFT uses request start (includes
preprocessing), the prometheus histogram also uses request start. The two
definitions were aligned in the Aug 24 fix. Non-streaming requests are
excluded from both (DB: `completionStartTime != endTime` guard; histogram:
only observed for streams). Historical p50s from the rollup histogram are
the right source for a `latency`-style field.

## 3. Findings (numbered, continuing the mapping doc series)

**8e. Realtime concurrency is unusable as-is.** The in-flight gauge leaks
inc-without-dec events (terminal logging skipped: client aborts, pre-log
exceptions, retry re-entry), producing frozen phantom values (690/644 on a
zero-traffic deployment; 418/335 on a busy one whose true peak is 9). Both
realtime surfaces read it. Until the leak is fixed at the source (or the
gauge is periodically reconciled against DB truth), no endpoint field
should be fed from it. The DB/rollup running-sum is the trustworthy
concurrency source at all horizons.

**8f. The two concurrency read surfaces disagree by construction** (max
across pods in `per_model` vs sum across pods in `/model/performance`,
690 vs 1334 observed for the same model in the same minute). Whichever
feeds the endpoints route must state its semantics; for a per-endpoint
(deployment) field, per-pod max is the honest one; the "sum" merge only
makes sense at model-group level.

**8g. Throughput semantics must match OpenRouter's.** `throughput_last_30m`
should be per-request generation speed (completion_tokens / request
duration), the DB/rollup definition. The prometheus counter rate is a
fleet capacity metric and under-reports speed under low load.

**8h. Best-fit sources for the telemetry fields** (all already persisted;
no new capture needed):
- `latency_last_30m`: p50 TTFT from the rollup ttft histogram, last 30
  minutes of buckets (streaming only; state that or fall back to p50
  `request_duration_ms` for non-streaming models).
- `throughput_last_30m`: rollup `throughput_tokens_sum /` active-time or
  per-request average from SpendLogs (same SQL the 24h path already runs,
  window shrunk to 30m; entity-scope filter not needed for the global
  field).
- `uptime_last_5m/30m/1d`: not concurrency at all; derive from the health
  table history the `gateway_status` resolver already reads (share of
  healthy checks in the window), or from `litellm_deployment_success/failure`
  counters (success / (success + failure)) which prod shows are accurate
  (GLM-5.3-Flash: 71,165 success / 194 failure since pod start).
- reserved `-1` status (SGLang load telemetry): the DB concurrency
  running-sum over a short window is the only trustworthy input; do not
  gate it on the prometheus gauge.

## 4. Proof-of-verification commands (read-only, prod)

- `GET /model/metrics/per_model?window=1h` -> 27 deployments; hamsa-stt
  concurrent flat 690; GLM-5.3-Flash flat 424.
- `GET /model/performance?window=1h` -> source=prometheus; hamsa-stt
  avg_concurrent 1334 flat; GLM-5.3-Flash group 752.
- `GET /model/performance?window=24h` -> source=rollup; GLM-5.3-Flash
  avg_concurrent 16.4, avg_throughput 101.1, p50_ttft 13.8s.
- PromQL range queries on the gauge: 7-day history for hamsa-stt shows a
  monotonic ratchet 10 -> 644 with zero traffic; GLM-5.3-Flash shows the
  ratchet coinciding with the 12:21-12:36 UTC failure burst.
- SQL ground truth (prod postgres, read-only): running-sum peak per model
  for the last hour; rollup starts vs ends per model; per-pod
  `deployment_*` counter sums vs gauge via each pod's authenticated
  `/metrics`.
