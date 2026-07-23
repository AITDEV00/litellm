# Tier 2 Dashboard Plan: Index

Status: analysis and planning complete.
Date: 2026-07-23.

## Documents

| Document | Purpose |
|----------|---------|
| `TIER2-LOGIC-MAP.md` | Maps every function, data flow, and data contract in the Tier 2 pipeline. Annotated with file paths and line numbers. Documents the 3 known bugs. |
| `TIER2-VSA-PLAN.md` | Defines the rebuild as 5 modularized vertical slices. Each slice has a goal, files to modify, tests, and a definition of done verifiable with curl. |
| `TIER2-TESTING-METHODOLOGY.md` | How to scrape live Prometheus data, load it for offline testing, and verify each step of the pipeline. Includes analysis results from the 2026-07-23 scrape. |
| `live-data/` | 13 scraped Prometheus responses + raw pod metrics + endpoint responses. Can be loaded as test fixtures. |

## Existing documents (from previous sessions)

| Document | Status |
|----------|--------|
| `DASHBOARD-EXTENSION-PROPOSAL.md` | Master decision doc (7 sections, tiered plan) |
| `TIER0-IMPLEMENTATION-NOTES.md` | Complete: Prometheus callback + ServiceMonitor |
| `TIER1-IMPLEMENTATION-NOTES.md` | Complete: ModelAnalytics tab (4 DB endpoints) |
| `TIER2-SCOPE.md` | Original scope doc (now superseded by the 3 new docs above) |
| `UI-LINT-AND-CHANGE-PROCESS.md` | UI development workflow |

## Current state

### What works

- Prometheus scrapes `/metrics/` every 30s (ServiceMonitor with trailing slash)
- 52 litellm metric names, 5023 time series flowing into Prometheus
- `GET /model/metrics/per_model?window=1h` returns 200 with 45 deployments
- 4 PromQL queries (concurrent, request_rate, output_tokens, latency_p50) return data
- NaN values are filtered out (no more JSON serialization errors)
- Timestamps use Unix epoch floats (no more double-timezone strings)
- `api_base` label normalization prevents new inc/dec mismatches
- UI component (`PerModelRealTimeView`) exists and renders deployment cards
- 30 unit tests passing across 3 test files

### What is broken

Nothing. The `litellm_deployment_in_progress_requests` gauge INC hook has been fixed. The INC now fires via `async_pre_call_deployment_hook`, which IS called by the router after deployment selection. The dead `log_pre_api_call` and `async_log_pre_api_call` overrides have been removed.

Stale negative gauge values from the pre-fix era will persist in Prometheus until the staleness window expires (5 minutes of no scrapes for those label series).

## Next step

Verify the gauge returns to 0 after a request using live curl against the deployed proxy.
