# Tier 1 Implementation: Thinking Process

Status: implemented on branch `jya0-v1.92.0`
Date: 2025-07-16

## Goal

Add a "Model Analytics" tab to the Usage page that consumes the four existing backend metrics endpoints (`/model/streaming_metrics`, `/model/metrics`, `/model/metrics/slow_responses`, `/model/metrics/exceptions`) without requiring any backend changes.

## Decision trail

### 1. Where to place the view

The Usage page already has five tabs (Cost, Model Activity, Key Activity, MCP Server Activity, Endpoint Activity). Adding a sixth tab here keeps analytics next to spend data, which is where operators already look. The alternative was the Models and Endpoints page, but that page is model-config focused (add/edit/delete deployments), not usage focused. The Usage page was the right home.

### 2. Which networking pattern to use

The codebase has two API call patterns in `networking.tsx`:

- **Legacy pattern** (150+ functions): raw `fetch()` with manual URL construction, manual header building, manual error handling, manual JSON parsing.
- **Modern pattern** (`apiClient.get/post/put/delete`): the shared HTTP client at `src/lib/http/client.ts` that handles base URL resolution, auth header injection, query param serialization, error parsing, and response deserialization.

The eslint config enforces `no-restricted-syntax` on raw `fetch()` outside `src/lib/http/`. The legacy functions predate the rule and are grandfathered via `eslint-suppressions.json`. Any new code touching `networking.tsx` triggers eslint on the full file, surfacing all 150+ pre-existing `fetch()` violations.

Decision: use `apiClient.get()` for all four new wrappers. This produces zero new lint errors and follows the modern pattern. The `apiClient` already handles `proxyBaseUrl`, `globalLitellmHeaderName`, query params, and `ApiError` throwing.

### 3. Component structure

A single `ModelAnalyticsView` component renders all four metrics in a 2x2 grid of Tremor cards. This matches the existing `SpendByProvider` and `EndpointUsage` components in the same directory.

The component takes four props: `accessToken`, `modelGroups`, `startTime`, `endTime`. The model group list is derived from the spend data already loaded by `UsagePageView` (via `day.breakdown.model_groups`), so no additional fetch is needed for the selector.

Each of the four endpoints gets its own `useQuery` hook with `enabled` guards on `accessToken`, `selectedModelGroup`, `startTime`, and `endTime`. This prevents unnecessary fetches when the date range is not yet selected.

### 4. Chart library choice

The entire Usage page uses `@tremor/react` for charts. The eslint config has a `no-restricted-imports` rule that flags `@tremor/react` as "being phased out; build new UI with antd instead." However, every existing component in `UsagePage/components/` uses Tremor and has a suppression entry in `eslint-suppressions.json`. Building the new component with antd charts would create visual inconsistency within the same page.

Decision: use Tremor to match the existing Usage page components, and add a suppression entry for the new file via `npx eslint --suppress-all`. This is the same approach taken for `SpendByProvider.tsx`, `TopKeyView.tsx`, `TopModelView.tsx`, `EntityUsage.tsx`, and `EndpointUsage.tsx`.

### 5. Dynamic vs static imports

The initial implementation used a dynamic `await import("@tremor/react")` pattern at module top level. This was incorrect because:

- It is not how any other component in the codebase works (all use static `import { BarChart, Card, ... } from "@tremor/react"`)
- Top-level `await` in a React component module is not standard and can break in some bundler configurations
- It adds unnecessary async overhead

Decision: use static imports, matching `SpendByProvider.tsx` exactly.

### 6. Type definitions

The four endpoints already exist in `schema.d.ts` (the auto-generated OpenAPI types). However, the networking wrappers use hand-written types rather than importing from the schema. This matches the existing pattern in `networking.tsx` where types like `ModelStreamingMetricsResponse`, `SlowResponsesRow`, and `ExceptionsResponse` are defined inline rather than derived from `schema.d.ts`.

The types are exported from `networking.tsx` so the component can import them directly.

### 7. Testing strategy

Seven focused tests covering:

1. Info alert when no date range selected (no API calls made)
2. All four card titles render plus model group selector
3. All four metric calls issued with correct params (model group, start/end time)
4. Partial failure warning when one endpoint rejects
5. Slow responses table with computed `% Slow` column
6. Empty-state messages when no data returned
7. No API calls when `accessToken` is null

The tests mock all four networking functions at the module level and use `QueryClientProvider` with `retry: false` to fail fast. The antd `Select` only renders the selected option in the DOM (not all options), so the test checks for the selected value only.

### 8. Model groups source

The `UsagePageView` already loads spend data that includes `day.breakdown.model_groups`. A `useMemo` extracts the unique sorted list of model group names from that data and passes it to the new component. No extra API call is needed.

If the spend data has no model groups (empty date range or no traffic), the selector will be empty and the component will show the "select a date range" info alert.

## Files changed

| File | Change |
|------|--------|
| `ui/litellm-dashboard/src/components/networking.tsx` | Added 4 type definitions + 4 `apiClient.get` wrappers + `buildMetricsQuery` helper (~90 lines at end of file) |
| `ui/litellm-dashboard/src/components/UsagePage/components/ModelAnalytics/ModelAnalyticsView.tsx` | New component: model group selector + 4 Tremor cards (2 BarCharts, 2 Tables) |
| `ui/litellm-dashboard/src/components/UsagePage/components/ModelAnalytics/ModelAnalyticsView.test.tsx` | 7 vitest unit tests |
| `ui/litellm-dashboard/src/components/UsagePage/components/UsagePageView.tsx` | Added import, `modelGroups` useMemo, 6th Tab + TabPanel |
| `ui/litellm-dashboard/eslint-suppressions.json` | Added suppression for `@tremor/react` import in new file |
| `ui/litellm-dashboard/eslint-metrics.json` | Regenerated (complexity count updated) |

## What was NOT done (deferred to later tiers)

- **Tier 0**: Enable Prometheus scraping of litellm-proxy via a ServiceMonitor (cluster has kube-prometheus-stack but no litellm scrape job)
- **Tier 2**: Add a `litellm_concurrent_requests` Gauge to the Prometheus integration and expose it on `/metrics`
- **Tier 3**: Add a `/model/metrics/top_consumers` endpoint that ranks API keys by request volume
- **Per-pod metrics**: Not in scope; "replica" in LiteLLM means one deployment (one `api_base`), not a Kubernetes pod

## Local testing

The Makefile at `oicm-litellm-layer/Makefile` provides rules for testing dashboard changes locally before building and deploying. See section 8 ("Local development and testing") in `DASHBOARD-EXTENSION-PROPOSAL.md` for the full workflow. The quick version:

1. `make litellm-local-run` — start the proxy from venv (Terminal 1)
2. `make litellm-ui-dev` — start the UI dev server with hot reload (Terminal 2)
3. Open `http://localhost:3000/ui/` and test the Model Analytics tab
4. `make litellm-src-build && make litellm-local-docker` — validate the full image
5. `make litellm-src-build-push && make deploy` — build, push, and deploy
