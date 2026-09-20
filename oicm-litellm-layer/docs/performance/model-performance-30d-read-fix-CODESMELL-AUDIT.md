# Model Performance 30d/YTD Read Fix — Code Smell Audit

Audit target: commit `7e410cffa3` (SQL coarse-bucket read path).

- `litellm/proxy/model_metrics_endpoints/model_performance_endpoints.py` — `_fetch_db_performance_from_rollup` and `_rollup_coarse_buckets_to_model`
- `tests/test_litellm/proxy/proxy_server/test_routes_model_performance.py` — coarse-bucket builder tests

Technique applied: the 4-layer code smell detection technique (`docs/techniques/code_smell_detection_technique.md`).

## L1 - automated tooling

Tools run on both files: `pyflakes`, `vulture`, and `ruff` with actionable rule sets (`F,E9,PLC,PLE,PLR,PLW,B,SIM,RET,ARG`, ignoring `PLR0913,PLR2004,SIM117,ARG002,ARG003,ARG005`).

- `pyflakes`: clean. No unused imports or undefined names.
- `vulture`: 4 findings, all false positives:
  - `user_api_key_dict` (route) — FastAPI `Depends` injection; used only for the auth side effect.
  - `prisma_with_query_raw`, `no_prisma` (tests) — fixtures consumed purely for their `monkeypatch` side effect.
- `ruff`: actionable findings only in pre-existing code outside this change (`PLW0603` global `_heavy_query_prisma_client`, `PLC0415` deferred imports, `PLR0917`/`ARG001` on the route signature). None introduced by the new code.

## L2 - per-file semantic checklist

- Single responsibility: `_rollup_coarse_buckets_to_model` only renders coarse rows to the response shape and folds histograms. SQL is kept in the fetcher. No mixing.
- No mutation of shared state: the builder folds into local `window_edges`/`window_counts`; `add_histogram_counts` returns a new list. No global writes in the new code.
- Errors modeled as values: `histogram_percentile` returns `None` for empty/invalid input instead of raising.
- Dead code: none in the new functions.

## L3 - cross-reference analysis

- SQL aliases (`request_count`, `completion_tokens`, `throughput_tokens`, `ttft_seconds_sum`, `ttft_histogram_edges`, `ttft_histogram_counts`, `concurrent_requests`, `bucket`, `model_group`) all match the keys the builder reads. Contract holds.
- `ttft_histogram_edges` comes back as NULL for the open upper edge through the SQL `MAX`; the builder restores it to `float("inf")`. Covered by `test_rollup_coarse_buckets_restore_null_infinity_edge`.
- `json_agg(ttft_histogram_counts)` yields a list of per-minute arrays; the builder folds them with `add_histogram_counts`. `zip(..., strict=True)` would raise on a length mismatch, but every bucket writes the fixed 32-bin `ttft_histogram_edges()`, so lengths always agree.
- `ttft_mean = ttft_seconds_sum / hist_total`: `build_ttft_histogram` returns all-zeros for `None` TTFT and only non-None TTFT adds to `ttft_seconds_sum`, so `hist_total` equals the count of TTFT-valid requests. Division is well-defined and never by zero (empty rows are skipped).

## L4 - fix-then-recheck loop

Re-ran the full test file after review: 16 passed, 0 failures. No new L1 findings introduced. No code change was required; the audit is conclusive.

## Verdict

The implementation is clean. The `ttft_mean` denominator and the percentile overwrite interplay were both investigated and confirmed correct. No new code smell or regression was found.