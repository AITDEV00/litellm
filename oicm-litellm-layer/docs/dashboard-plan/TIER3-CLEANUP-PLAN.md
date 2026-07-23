# Tier 3: Dashboard Cleanup and Per-Model Chart Plan

Status: implementing.
Date: 2026-07-23.

## Problem statement

Two issues on the [LiteLLM Dashboard](https://litellm.adeoaiengine.ecouncil.ae/ui/usage/) usage page:

### A. Usage page model bloat (Cost / Model Activity / Key Activity tabs)

The Cost tab "Top Models" card and the Activity tabs derive their model list from `userSpendData.results[].breakdown.model_groups`, which comes from spend logs. Spend logs accumulate every model string ever used in an API call, including:

Models deleted from the proxy config long ago (e.g. `zai-org/GLM-5.1-FP8`, `hosted_vllm/MiniMaxAI/MiniMax-M3-MXFP8-no-think`)
Test / fake models (`FAKE-NONEXISTENT-MODEL-XYZ`, `fake-model-test-xyz`, `test`, `nonexistent-model-xyz`)
Models called with inconsistent naming (sometimes with `hosted_vllm/` prefix, sometimes without)

Result: 80+ models listed in the Cost tab, but only 24 are currently registered in the proxy. The user wants these separated into "currently valid" (registered) and "invalid/historical" (not registered).

### B. Real-Time Per Model tab is bugged

The deployment dropdown is glitchy because the endpoint returns 60 "deployments" when there are only 24 registered models. This is caused by cross-metric label fragmentation:

The 4 PromQL queries in `get_per_model_metrics` group by `(model_id, litellm_model_name, api_base, api_provider)`, but each underlying Prometheus metric has different label values for the same `model_id`:

| Metric | `litellm_model_name` | `api_base` |
|--------|---------------------|------------|
| `litellm_deployment_in_progress_requests` (gauge) | prefixed (`hosted_vllm/zai-org/GLM-5.2-FP8`) | normalized (`.../v1`) |
| `litellm_deployment_total_requests_total` (counter) | unprefixed (`zai-org/GLM-5.2-FP8`) | endpoint-suffixed (`.../v1/chat/completions`) |
| `litellm_output_tokens_metric_total` (counter) | **missing** (has `model` instead) | **missing** |
| `litellm_deployment_latency_per_output_token_bucket` (histogram) | unprefixed | endpoint-suffixed |

So a single deployment like `e2acef83` appears as 6+ separate entries, each with only one of the 4 time series populated. The dropdown shows all 60, making it unusable.

The user also wants the Real-Time Per Model tab to reuse the registered model list (from the Models + Endpoints page) as the filtering source, so they can pick a registered model and see its real-time metrics in a visible chart.

## Architecture decisions

### Decision 1: Group by `model_id` only in the backend

`model_id` is the only label present and consistent across all 4 Prometheus metrics. Change the PromQL `sum by` clauses and the `_extract_deployment_key` function to use `model_id` as the sole grouping key. Merge `litellm_model_name`, `api_base`, and `api_provider` from whichever query has the most complete label set (the gauge, since it has all 4 labels populated correctly after the Bug 4 fix).

### Decision 2: Cross-reference spend log models against registered models in the frontend

The usage page already has access to the spend log model list (`modelGroups` derived from `userSpendData`). Add a parallel fetch of registered models via `modelInfoCall` (already exists in `networking.tsx`). Build a set of valid identifiers (both `model_name` and `litellm_params.model` for each registered model). Split the spend log model list into "registered" and "historical" groups. Show registered by default; provide a toggle to show historical.

### Decision 3: Reuse `modelInfoCall` for the Real-Time Per Model dropdown

The `PerModelRealTimeView` component currently builds its dropdown from the fragmented endpoint response. Instead, fetch registered models via `modelInfoCall` (same as the Models + Endpoints page) and use those as dropdown options. When a model is selected, pass its `model_info.id` as the `model_id` query parameter to `/model/metrics/per_model`.

### Decision 4: Use @tremor/react AreaChart for time-series visualization

The dashboard already uses `@tremor/react` (v3.18.7) for charts (`BarChart`, `DonutChart`). Use `AreaChart` for the 4 time series (concurrent requests, request rate, output tokens/sec, latency p50). Each gets its own chart card within the selected model's deployment card.

### Decision 5: Frontend testing process

Use the existing vitest + jsdom setup (`vitest.config.ts`, `tests/test-utils.tsx` with `renderWithProviders`). Test strategy:

1. **Unit tests**: Mock the API calls (`perModelMetricsCall`, `modelInfoCall`), render the component, verify dropdown options come from registered models, verify charts render with mocked time-series data, verify filtering works
2. **Integration tests**: Test the full `PerModelRealTimeView` with mocked API responses that simulate the label fragmentation (multiple entries for same `model_id`), verify they get merged into one card
3. **Live verification**: After deploy, curl the endpoint and check the deployment count matches registered model count, then open the browser and verify the dropdown, chart rendering, and model selection flow

## Implementation phases

### Phase 1: Backend — fix label fragmentation

**File**: `litellm/integrations/prometheus_helpers/prometheus_api.py`

1. Change `_DEPLOYMENT_LABELS` from the 4-label tuple to just `("model_id",)` for the `sum by` clauses in the PromQL queries
2. Change `_extract_deployment_key` to return just `model_id` (a single string, not a tuple)
3. Change `_empty_deployment_dict` to take a single `model_id` string
4. After collecting all 4 time series, do a label-merge pass: for each `model_id`, pick the non-empty `litellm_model_name`, `api_base`, `api_provider` from whichever series has them (the gauge series will have the most complete set)
5. The `concurrent_requests` query (gauge) should keep its current grouping since `max_over_time` doesn't use `sum by` — but the key extraction should still be just `model_id`
6. The `output_tokens_per_sec` query needs `label_replace` to map the `model` label to `litellm_model_name` so the merge pass can pick it up, OR we just accept that this series won't have `litellm_model_name` and rely on the gauge series for it

**File**: `litellm/proxy/model_metrics_endpoints/per_model_endpoints.py`

7. No changes needed — the endpoint just passes through the response from `get_per_model_metrics`

**Tests**: Update `tests/test_litellm/integrations/test_prometheus_per_model_api.py` to verify that fragmented responses (multiple label sets for same `model_id`) get merged into a single deployment entry

**Verification**: After deploy, curl `/model/metrics/per_model?window=1h` and confirm deployment count matches the number of distinct `model_id` values in Prometheus (should be ~24, not 60)

### Phase 2: Frontend — fix PerModelRealTimeView dropdown and filtering

**File**: `ui/litellm-dashboard/src/components/UsagePage/components/PerModelRealTime/PerModelRealTimeView.tsx`

1. Add a `useModelsInfo` hook call (from `@/app/(dashboard)/hooks/models/useModels`) to fetch registered models
2. Replace the deployment dropdown options: instead of building from the endpoint response, build from registered models. Each option's value is `model_info.id`, label is `model_name` (or `litellm_params.model` for disambiguation)
3. When a model is selected from the dropdown, pass its `model_info.id` as `model_id` to `perModelMetricsCall`
4. Add an "All registered models" option that doesn't pass `model_id` (shows all deployments)
5. Keep the time window selector as-is

**Tests**: Create `PerModelRealTimeView.test.tsx` with:
- Mock `modelInfoCall` returning 3 registered models
- Mock `perModelMetricsCall` returning 3 deployments (one per model_id)
- Verify dropdown shows the 3 registered model names
- Verify selecting a model filters the cards
- Verify "All" option shows all cards

### Phase 3: Frontend — build visible charts

**File**: `ui/litellm-dashboard/src/components/UsagePage/components/PerModelRealTime/PerModelRealTimeView.tsx`

1. Import `AreaChart` from `@tremor/react`
2. For each deployment card, replace the plain-text metric display with 4 `AreaChart` components (one per time series)
3. Each chart: x-axis = timestamp, y-axis = value, with appropriate formatting (concurrent = integer, rate = `/s`, tokens = `tok/s`, latency = `ms`)
4. Charts should be responsive and use the existing Tremor styling
5. Keep the "Show time-series data points" details section as a fallback/debug view

**Tests**: Update `PerModelRealTimeView.test.tsx`:
- Mock time-series data with multiple points
- Verify charts render (check for SVG/canvas elements or Tremor chart class names)
- Verify empty data state (no chart, shows "No data")

### Phase 4: Frontend — usage page model bloat cleanup

**File**: `ui/litellm-dashboard/src/components/UsagePage/components/UsagePageView.tsx`

1. Add a `useModelsInfo` hook call to fetch registered models (page 1, size 100 to get all)
2. Build a `validModelIdentifiers` set from the registered models: for each model, add both `model_name` and `litellm_params.model` to the set
3. In the `topModels` and `topModelGroups` useMemo calculations, split into two arrays: `topModelsRegistered` and `topModelsHistorical` (same for model groups)
4. In the "Top Models" card UI, show registered models by default. Add a toggle/button: "Show historical models" that reveals the historical list
5. Apply the same filtering to the Activity tabs (`modelMetrics`): filter to registered models by default, with a toggle

**Tests**: Update `UsagePageView.test.tsx`:
- Mock `modelInfoCall` returning 3 registered models
- Mock spend data with 5 models (3 registered, 2 historical)
- Verify only 3 models show by default
- Verify toggle reveals all 5

### Phase 5: Testing — frontend testing process

**File**: `oicm-litellm-layer/docs/dashboard-plan/FRONTEND-TESTING-PROCESS.md`

Document the process:

1. **Mock API layer**: Mock all networking calls (`modelInfoCall`, `perModelMetricsCall`, `userDailyActivityCall`) at the module level using `vi.mock`
2. **Render with providers**: Use `renderWithProviders` from `tests/test-utils.tsx` to get Redux + router context
3. **Assert on DOM**: Use `@testing-library/react` queries (`screen.getByText`, `screen.getByRole`) to verify rendered output
4. **Simulate user interaction**: Use `fireEvent` or `userEvent` to click, select, type
5. **Verify API calls**: Use `vi.fn()` mocks and `expect(mockFn).toHaveBeenCalledWith(...)` to verify correct parameters
6. **Test edge cases**: Empty data, error states, loading states, Prometheus not connected
7. **Run**: `cd ui/litellm-dashboard && npx vitest run src/components/UsagePage/`

### Phase 6: Live verification

1. Build and deploy: `cd oicm-litellm-layer && make litellm-src-build-push`
2. Deploy: `kubectl rollout restart deployment/litellm-proxy -n mlops`
3. Port-forward: `kubectl -n mlops port-forward litellm-proxy-<pod> 4002:4000`
4. Verify backend: `curl -s "http://localhost:4002/model/metrics/per_model?window=1h" -H "Authorization: Bearer sk-1234" | python3 -c "..."` — deployment count should match registered model count
5. Verify frontend: Open browser to `http://localhost:4002/ui/?page=usage`, click "Real-Time Per Model" tab, verify dropdown shows registered model names, select a model, verify charts render
6. Verify usage page bloat: Open Cost tab, verify only registered models appear by default, verify toggle shows historical models

## Execution order

Phase 1 (backend fragmentation fix) is the foundation — without it, the frontend changes won't work correctly. Phases 2-3 depend on Phase 1. Phase 4 is independent. Phase 5 runs alongside Phases 2-4. Phase 6 runs after all phases.

```
Phase 1 (backend) ──> Phase 2 (dropdown) ──> Phase 3 (charts) ──┐
                                                                 ├──> Phase 6 (live verify)
Phase 4 (bloat cleanup) ─────────────────────────────────────────┘
                                                                
Phase 5 (testing) runs alongside Phases 2-4
```

## Files to modify

### Backend
- `litellm/integrations/prometheus_helpers/prometheus_api.py` — fix label fragmentation
- `tests/test_litellm/integrations/test_prometheus_per_model_api.py` — update tests

### Frontend
- `ui/litellm-dashboard/src/components/UsagePage/components/PerModelRealTime/PerModelRealTimeView.tsx` — dropdown fix + charts
- `ui/litellm-dashboard/src/components/UsagePage/components/PerModelRealTime/PerModelRealTimeView.test.tsx` — new test file
- `ui/litellm-dashboard/src/components/UsagePage/components/UsagePageView.tsx` — model bloat cleanup
- `ui/litellm-dashboard/src/components/UsagePage/components/UsagePageView.test.tsx` — update tests

### Docs
- `oicm-litellm-layer/docs/dashboard-plan/FRONTEND-TESTING-PROCESS.md` — new
