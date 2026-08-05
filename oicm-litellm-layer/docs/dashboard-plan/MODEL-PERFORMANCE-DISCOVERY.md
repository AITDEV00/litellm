# Model Performance Tab — Frontend & Backend Discovery

Status: discovery + decisions recorded (no code changes made yet).
Branch: `jyao-v1.95.0`. Date: 2026-08-05.

This document maps the current state of the Model Performance tab in the
`/ui/usage` path, the reusable components available, and the gaps for four
requests (usage-view scoping, concurrent-request data, model dropdown, chart
colors/hover, and a key-level concurrent drill-down). It follows the
`FRONTEND-UNDERSTANDING-TECHNIQUE.md` and `LOGIC-MAPPING-TECHNIQUE.md`
workflows. Nothing has been implemented.

---

## 0. Decisions

This section records the decisions taken during discovery. Each entry captures
the decision, the reasoning, and the implementation impact. They are the source
of truth for the subsequent implementation work.

### 0.1 Model Performance tab must not be gated

**Decision:** The Model Performance tab should be available in **all** usage
views (global, my-usage, organization, team, customer, tag, agent, user, and
user-agent-activity), not gated to only global / my-usage.

**Why:** Model performance is a first-class usage signal that operators need
regardless of which usage view they are looking at. The current gating to
`(usageView === "global" || usageView === "my-usage")` is a legacy artifact of
the original feature commit (see section 1.6), not a product requirement or a
technical limitation. There is no platform/component restriction preventing
`ModelPerformanceView` from rendering in other views.

**Implications:**
- The backend endpoint must accept entity scope filters (`team_id`, `user_id`,
  `end_user`, `api_key` / `customer_id`, etc.) so the data returned respects the
  selected entity. Today the endpoint only accepts `window` and `model_group`,
  and ignores the authenticated `user_api_key_dict` entirely.
- `useModelPerformance` and `modelPerformanceCall` must thread those entity
  scope params through to the endpoint.
- `ModelPerformanceView` must be rendered for every usage view (either by adding
  a "Model Performance" tab to `EntityUsage`, or by rendering it per-view).
- The endpoint and hook must **never** leak cross-entity data: an entity-scoped
  request must return only that entity's metrics (and, where an entity has no
  performance data, show an empty state rather than global data).

**Resolved sub-decisions:**

- **A — per-entity tab vs single filtering tab: RESOLVED → A1 (per-entity tab).**
  The global/my-usage view and the entity views use two *different* tab bar
  implementations. Under A1 we add a "Model Performance" tab inside `EntityUsage`
  (alongside Cost / Model Activity / Key Activity / Endpoint Activity), so each
  entity view gets its own scoped Model Performance tab, mirroring how the other
  tabs already repeat per entity. (The rejected A2 "single filtering tab" would
  keep one tab in the global bar and add a scope selector inside the chart.)
  A1 is chosen because it is more consistent with the existing pattern and
  reuses `EntityUsage`. Keep the existing "Model Performance" tab in the
  global/my-usage `TabGroup` as well.
- **B. Should "my-usage" scope to the user's own keys/teams: RESOLVED → B1
  (keep global data).** "My Usage" continues to show global model performance;
  it does not restrict to the authenticated user's own keys/teams. Rationale:
  an admin logs into the dashboard anyway, and a non-admin cannot access the
  global scope, so per-user data isolation is not required for this view. This
  means no additional per-user scoping logic is needed for the `my-usage` path
  beyond the existing global behavior.

**Open sub-decisions (still to confirm during implementation):**
- How the admin/non-admin role check gates the *global* scope (the model
  performance data) versus entity-scoped data. The endpoint should return an
  entity-scoped result for entity views, and a global result for the
  global/my-usage views only when the caller is an admin (mirroring how the rest
  of the usage page gates global data via `isAdmin`).

### 0.2 Concurrent requests data must be real (not hardcoded 0)

**Decision:** Fix the concurrent-requests time series and summary so they
reflect real values instead of hardcoded `0.0`. See section 2 for the full bug
analysis.

**Why:** The concurrent line is always `0` in both the DB and Prometheus paths,
which is misleading to operators and hides real load.

**Implications:**
- DB path: define a policy for deriving a concurrency signal from
  `LiteLLM_SpendLogs` (e.g. count of overlapping request windows), or clearly
  mark the DB-sourced concurrent value as "N/A" rather than `0`. Do not hardcode
  `0.0`.
- Prometheus path: group by a label that exists on the metric. Reuse the proven
  `model_id`-grouped query from `prometheus_api.py:318`, or add a
  `requested_model` label to the concurrent gauge and TTFT histogram and emit it.
- Do not force `avg_concurrent = 0.0` in `_compute_summaries` / `_finalize_summary`.

### 0.3 Model dropdown should be a searchable checkbox multi-select (see section 3)

### 0.4 Chart colors + hover must be distinct and informative (see section 4)

### 0.5 Concurrent chart needs a key-level drill-down (see section 5)

---

## 1. Where the tab lives and how it is gated (Request 1)

### 1.1 Component

`ui/litellm-dashboard/src/components/UsagePage/components/ModelPerformance/ModelPerformanceView.tsx`

A self-contained `React.FC` that:

- holds its own window state (`Segmented` 5m/15m/1h/24h/7d, default `1h`) and a
  single-select `model_group` filter
- calls `useModelPerformance(window, selectedModelGroup)`
- renders three `@tremor/react` `LineChart` cards (Concurrent Requests,
  Throughput, TTFT) and a Summary table

### 1.2 Data hook

`ui/litellm-dashboard/src/app/(dashboard)/hooks/models/useModelPerformance.ts`

```ts
export const useModelPerformance = (window = "1h", modelGroup?) => {
  return useQuery<ModelPerformanceResponse>({
    queryKey: performanceKeys.list({ filters: { window, ...(modelGroup ? { modelGroup } : {}) } }),
    queryFn: () => modelPerformanceCall(accessToken, window, modelGroup),
    enabled: Boolean(accessToken),
    placeholderData: keepPreviousData,
    refetchInterval: ["5m", "15m"].includes(window) ? 30_000 : false,
  });
};
```

No entity scoping (team / customer / user / key / tag). The hook and the
network wrapper take only `window` and `model_group`.

### 1.3 API wrapper

`ui/litellm-dashboard/src/components/networking.tsx` (`modelPerformanceCall`, ~line 1776):

```ts
apiClient.get(`/model/performance`, { accessToken, query: { window, ...(modelGroup ? { model_group: modelGroup } : {}) } });
```

### 1.4 Routing / gating in the usage page

`ui/litellm-dashboard/src/app/(dashboard)/usage/_components/components/UsagePageView.tsx`

The Model Performance tab is rendered **only** inside the `TabGroup` that is
gated by:

```tsx
{(usageView === "global" || usageView === "my-usage") && (
  <TabGroup>
    <TabList>
      <Tab>Cost</Tab>
      <Tab>Model Activity</Tab>
      <Tab>Key Activity</Tab>
      <Tab>MCP Server Activity</Tab>
      <Tab>Endpoint Activity</Tab>
      <Tab>Model Performance</Tab>   <-- tab 6
    </TabList>
    ...
    <TabPanel><ModelPerformanceView /></TabPanel>
  </TabGroup>
)}
```

Other `usageView` values (organization, team, customer, tag, agent, user,
user-agent-activity) render `EntityUsage` or `UserAgentActivity`, which have
**no** Model Performance tab.

`ui/litellm-dashboard/src/app/(dashboard)/usage/_components/components/EntityUsage/EntityUsage.tsx`

Its tab list (lines 689-705) is:

```
Cost
Model Activity (or "Request / Token Consumption" for agent)
Agent Activity   (team only)
Key Activity
Endpoint Activity
```

It reads spend via `ENTITY_FETCH_FNS` (`tagDailyActivityCall`,
`teamDailyActivityCall`, `organizationDailyActivityCall`,
`customerDailyActivityCall`, `agentDailyActivityCall`,
`userDailyActivityCall`) and `usePaginatedDailyActivity`. All entity data flows
through this one component; only `entityType` differs.

**Root cause of Request 1:** `ModelPerformanceView` is only mounted under the
` "my-usage" / "global"` `TabGroup`. To show it in other usage views you either
(a) add a "Model Performance" tab to `EntityUsage` and give `ModelPerformanceView`
/ `useModelPerformance` / `modelPerformanceCall` / the backend an entity scope
filter, or (b) mount it per view. Option (a) is the DRY path because
`EntityUsage` already centralizes all entity views.

**Decision (section 0.1):** the tab must **not** be gated. Implementation will
follow option (a): add a "Model Performance" tab to `EntityUsage` (and keep it in
the global/my-usage tab list), and thread entity scope filters from
`ModelPerformanceView` -> `useModelPerformance` -> `modelPerformanceCall` ->
the backend endpoint so each usage view sees only its own entity's data.

### 1.6 Why is it gated? Is it a limitation or a faulty prior path?

**It is neither a component/backend limitation nor the upstream code — it is the
scoping decision made in the feature commit itself, and the "faulty prior path"
you suspected is confirmed.**

- `git log --all -- <ModelPerformanceView.tsx>` shows only two commits: the
  feature commit `1d633c35fc` ("feat(observability): add per-model performance
  endpoint and dashboard tab") and the merge `3a262693e1`. The endpoint, hook,
  networking wrapper, and tab all originate from `1d633c35fc`.
- That commit did **not** touch `UsagePageView.tsx` (the `git show
  1d633c35fc -- UsagePageView.tsx` diff is empty). The tab was added to the
  shared global `TabGroup` only later, via the merge `3e262693e`.
- The backend endpoint `model_performance()` accepts **only** `window` and
  `model_group` query params. There is no `team_id` / `user_id` / `end_user` /
  `api_key` filter. `user_api_key_dict` is captured from the auth dependency but
  **never used**, so the data is always global regardless of who calls it.
- The tab was dropped into the `(usageView === "global" || usageView ===
  "my-usage")` `TabGroup` because that is the only tab group `ModelPerformanceView`
  was wired into. `EntityUsage` (which serves every other view) was never given
  a Model Performance tab.

So there is no platform restriction on which components can be used. The gap is
in the feature's own frontend wiring + backend contract. To support team /
organization / customer / user scoping, both need an entity-scope parameter,
and `EntityUsage` needs a tab.

**Decision (section 0.1):** proceed with un-gating — add the tab to
`EntityUsage` and add entity-scope params end to end.

### 1.5 `usageView` options

`ui/litellm-dashboard/src/app/(dashboard)/usage/_components/components/UsageViewSelect/UsageViewSelect.tsx`

`UsageOption = "global" | "my-usage" | "organization" | "team" | "customer" | "tag" | "agent" | "user" | "user-agent-activity"`.

`EntityType` (for `EntityUsage`) is `"tag" | "team" | "organization" | "customer" | "agent" | "user"` (`ui/litellm-dashboard/src/components/EntityUsageExport/types.ts`).

---

## 2. Concurrent requests is always 0 (Request 2)

This is a real bug, and it is **two separate defects** depending on which data
source the endpoint falls back to.

### 2.1 Backend endpoint

`litellm/proxy/model_metrics_endpoints/model_performance_endpoints.py`

Route `GET /model/performance`. It branches:

```python
if window in _PROMETHEUS_WINDOWS:            # ("5m", "15m", "1h")
    prom_result = await _fetch_prometheus_performance(...)
    if prom_result is not None:
        return prom_result
return await _fetch_db_performance(...)
```

`_PROMETHEUS_WINDOWS = ("5m", "15m", "1h")`. Note `1h` is in the Prometheus set,
so the DB path only runs for `24h` / `7d` (or whenever Prometheus is not
connected).

### 2.2 Defect A — DB path hardcodes concurrent = 0

In `_fetch_db_performance`, the SQL computes throughput and TTFT but has no
concurrency signal, so it appends:

```python
models[mg]["time_series"]["concurrent_requests"].append({"timestamp": ts_bucket, "value": 0.0})
```

i.e. `24h` and `7d` windows always show a flat 0 concurrent line. The `avg_concurrent`
summary is also hardcoded:

```python
def _compute_summaries(models):
    ...
    mg_data["summary"]["avg_concurrent"] = 0.0
```

So **even in the Prometheus path**, `avg_concurrent` is forced to `0.0` in the
summary table (only the time-series array gets real data).

### 2b. Defect B — Prometheus query label mismatch (concurrent AND TTFT)

The endpoint's three Prometheus queries all group by `requested_model`. Whether
that works depends on each metric's actual label set **at emission time**, and
it differs per metric:

| Metric | Defined labels | `requested_model` present? |
|--------|----------------|--------------------------|
| `litellm_deployment_in_progress_requests` (concurrent) | `litellm_model_name, model_id, api_base, api_provider` (`prometheus.py:531`) | **No** |
| `litellm_deployment_latency_per_output_token` (TTFT) | `litellm_model_name, model_id, api_base, api_provider, api_key_hash, api_key_alias, team, team_alias` (`prometheus.py:376`) | **No** |
| `litellm_output_tokens_metric` (throughput) | includes `requested_model` + `model_id` (`prometheus.py:487`) | **Yes** |

Confirmed at emission:

- Concurrent gauge emitted with only `litellm_model_name / model_id / api_base /
  api_provider` in `_inc_deployment_in_progress` (`prometheus.py:1259`); decremented
  at `:2634` and `:2881`.
- TTFT histogram observed in `set_llm_deployment_success_metrics` (`prometheus.py:2908`)
  with a label set that has **no** `requested_model`.
- Output-token counter **does** get `requested_model` (from
  `standard_logging_payload["model_group"]`, set at `prometheus.py:1320`).

So in Prometheus mode (`5m` / `15m` / `1h`), **concurrent and TTFT are both
broken** — `max by (requested_model)` / `sum by (le, requested_model)` match a
label that doesn't exist, returning nothing. **Only throughput actually works.**
This is a broader defect than the earlier "concurrent only" note.

Two correct approaches already exist in the repo:

- `litellm/integrations/prometheus_helpers/prometheus_api.py:318`
  `get_per_model_metrics` groups by `model_id`:
  ```python
  "concurrent_requests": f"max by (model_id) (max_over_time(litellm_deployment_in_progress_requests{label_filter}[{range_str}]))"
  ```
  This is the proven, correct query. The model-performance endpoint should use
  the same approach: group by `model_id` (and map to a display name), or add a
  `requested_model` label to the concurrent gauge and the latency histogram and
  emit it.

### 2c. The existing implementation plan already flagged this

`oicm-litellm-layer/docs/dashboard-plan/OBSERVABILITY-IMPLEMENTATION-PLAN.md`
section 4.3.1 item 2 explicitly records that the 7 core metrics
(`litellm_deployment_*`) do **not** carry a `model_group` label, only 4 other
metrics do, and that grouping by `model_group` requires either adding the label
or aggregating by `model_id` and joining via `_get_deployment_label_metadata`.
Sections D.7 / E.2 also confirm the label set and the "stuck gauge" behavior.

### 2d Fix shape (for later implementation, not done here)

- DB path: keep the summary + time-series for concurrent at a meaningful value
  (there is no per-request concurrency signal in `LiteLLM_SpendLogs`, so this
  needs either a defined policy, e.g. count of overlapping request windows, or a
  clear "N/A" in the UI).
- Prometheus path: switch the concurrent query to group by a real label
  (`model_id`) or add the missing `requested_model` / `model_group` label to the
  gauge in `types/integrations/prometheus.py` and emit it in `prometheus.py`.
- Un-hardcode `avg_concurrent` in `_compute_summaries`.

---

## 3. Model dropdown: wants checkbox multi-select + search (Request 3)

The current dropdown is a single-select:

```tsx
<Select style={{ width: 220 }} placeholder="Select model group"
  value={selectedModelGroup || ""} onChange={...}
  options={modelGroupOptions} allowClear />
```

where `modelGroupOptions` is built from `data.models[].model_group` and a
leading `{ label: "All Models", value: "" }`.

### Reusable components that already do this

- `ui/litellm-dashboard/src/components/common_components/team_multi_select.tsx`
  — Ant `Select mode="multiple" showSearch` with `filterOption={false}`,
  server-side debounced search (`useDebouncedState`), infinite scroll
  (`onPopupScroll`), `allowClear`, `loading` / `notFoundContent`. This is the
  closest ready-made pattern for a searchable multi-select.
- `ui/litellm-dashboard/src/components/ModelSelect/ModelSelect.tsx` — a
  `mode="multiple"` `Select` with grouped options (Special / Wildcard / Models),
  `maxTagCount="responsive"`, and search. Heavier than needed (fetches proxy
  models, teams, orgs, current user via hooks).
- `ui/litellm-dashboard/src/components/common_components/ModelSelector.tsx` —
  single-select with `showSearch` + custom-model input.
- Numerous `mode="multiple"` usages across the app (e.g. `add_pass_through.tsx`,
  `GuardrailSelector.tsx`, `PolicySelector.tsx`, `MCPServerSelector.tsx`,
  `SearchToolSelector.tsx`, `AgentSelector.tsx`).

**Recommended path:** reuse the `team_multi_select.tsx` interaction pattern
(Ant `Select mode="multiple" showSearch`) but render checkboxed options.
Ant `Select mode="multiple"` already renders a check icon on selected options.
For an explicit checkbox list, `components/ui` has shadcn primitives; a simpler
path is Ant `Select` + `optionRender`. The `ModelPerformanceView` already
imports `antd` `Select`, so a multi-select can be built with minimal new code.
Add a `searchInput`/debounce if the model list is large.

`ModelSelect.tsx` demonstrates the "selectable from available proxy models"
data source via `useAllProxyModels`; if the dropdown should list all proxy
model groups rather than only those that already emitted performance data, that
hook is the reusable source.

---

## 4. Chart colors, distinctness, hover (Request 4)

### Current chart

`ModelPerformanceView` uses `@tremor/react` `LineChart` with:

```tsx
const TREMOR_COLORS = ["blue","cyan","indigo","violet","purple","fuchsia","pink","rose","red","orange"];
colors={TREMOR_COLORS.slice(0, categories.length)}
```

Only 10 colors, several visually similar (`blue`/`cyan`/`indigo`,
`red`/`rose`/`pink`). Tremor `LineChart` has no `customTooltip` (it is the older
Tremor API) and no per-line hover highlight beyond the default.

### Reusable chart infrastructure (the DRY path)

`ui/litellm-dashboard/src/components/shared/charts/`

- `colors.ts` exports `CHART_COLOR_HEX` (23 named colors), `DEFAULT_COLOR_CYCLE`
  (22 distinct colors), `SEQUENTIAL_COLOR_RAMP`, and `categoryFills(count, colors)`
  which cycles the palette and expands `#`-hex. This is the reusable, larger,
  more distinct palette to use instead of the 10 Tremor colors.
- `line_chart.tsx` exports a typed `LineChart` on top of Recharts with
  `colors`, `customTooltip`, `connectNulls`, `curveType`, `showLegend`,
  `showTooltip`, and `ValueTooltip` default. It renders each category as a
  `Line` and a `ChartLegend`.
- `chart_tooltip.tsx` exports `ValueTooltip` (shows the category `name` and a
  color swatch) and `ChartTooltipComponent` type — this satisfies "on hover,
  show exactly which model this line is and highlight the line."
- `ui/chart.tsx` provides `ChartContainer`, `ChartLegend`, `ChartLegendContent`,
  `ChartTooltip` (Recharts wrappers, active-state cursor styling).

So the recommendation is to **swap the Tremor `LineChart` for the shared
`line_chart.tsx`**, pass `DEFAULT_COLOR_CYCLE` (or a subset that guarantees
distinct hues before repeating), and provide a custom tooltip / leverage
`ValueTooltip`. That single change gives bigger, more distinct colors + model
name in the hover tooltip. For line highlight on hover, Recharts `Line`
`activeDot`/`strokeWidth` and the `ChartLegend` active state in
`ui/chart.tsx` are the building blocks.

---

## 4. Concurrent drilldown → which keys sent requests (Request 5)

The request: once concurrent requests is fixed, clicking a highlighted time
point in the concurrent chart should open a view of which keys sent requests at
that time, opened like the `/ui/logs/` drawer.

### Existing reusable pieces

- **The logs drawer**: `ui/litellm-dashboard/src/components/view_logs/LogDetailsDrawer/LogDetailsDrawer.tsx` is opened from `RequestLogsPanel.tsx`
  (`ui/litellm-dashboard/src/components/view_logs/RequestLogsPanel.tsx`) via
  `setSelectedLog(log)` + `setIsDrawerOpen(true)`, with `onKeyHashClick` /
  `onSessionClick`. The `LogDetailsDrawer` is the exact "drawer that shows a
  log's detail" the user wants to match.
- **Log entry shape**: `columns.tsx` `LogEntry` includes `api_key`, `user`,
  `end_user`, `team_id`, `model`, `model_group`, `request_duration_ms`,
  `startTime`, `endTime`, `spend`, `tokens`, `metadata` (incl. `user_api_key`,
  `user_api_key_alias`), and `computeThroughput()`.
- **Logs API**: `uiSpendLogsCall` (`networking.tsx:1992`) hits
  `/spend/logs/ui` with `UiSpendLogsParams` (`api_key`, `team_id`, `user_id`,
  `end_user`, `model`, `model_id`, `session_id`, `start_date`/`end_date`,
  pagination). This is the endpoint to reuse to fetch the keys that sent
  requests in a time range/model.
- **Key-level metrics**: `ui/litellm-dashboard/src/components/activity_metrics.tsx`
  `ActivityMetrics` + `processActivityData(data, "api_keys", teams)` already
  produces per-key breakdowns, and `ActivityMetrics` renders "Top Virtual Keys
  by Spend". `KeyModelUsageView.tsx` shows model usage per key. These are
  reusable to render "which keys were sending requests" inside the drawer.
- **Entity-scoped data**: `usePaginatedDailyActivity` and the `*DailyActivityCall`
  functions support entity filters; `EntityUsage` already shows per-key
  breakdown.

### The clean integration shape

Build a reusable component (e.g. `ConcurrentRequestsDrilldown`) that, given a
`model_group` + time window, uses the `LogDetailsDrawer` shell (or the
`RequestLogsPanel` table) filtered by `model` + `start_date`/`end_date` via
`fetchLogsCall`. This matches the existing logs-page experience ("open it like
I see on /ui/logs/"). The shared `ModelSelector`/multi-select patterns and
`ActivityMetrics` key breakdown can be reused for the "which keys" section.

---

## 6. Reusable-component inventory (the mapping scratchpad)

Confirmed reusable pieces relevant to this feature:

| Need | Existing reusable piece | Location |
|------|-------------------------|----------|
| Multi-select + search dropdown | `team_multi_select.tsx` pattern | `components/common_components/team_multi_select.tsx` |
| Multi-select models (all proxy models) | `ModelSelect.tsx` | `components/ModelSelect/ModelSelect.tsx` |
| Single model selector | `ModelSelector.tsx` | `components/common_components/ModelSelector.tsx` |
| Distinct color palette | `DEFAULT_COLOR_CYCLE` (22), `CHART_COLORS` | `components/shared/charts/colors.ts` |
| Line chart with tooltip/colors | `LineChart` + `ValueTooltip` | `components/shared/charts/line_chart.tsx`, `chart_tooltip.tsx` |
| Chart shell (legend/cursor) | `ChartContainer`/`ChartLegend` | `components/ui/chart.tsx` |
| Log detail drawer | `LogDetailsDrawer` | `components/view_logs/LogDetailsDrawer/LogDetailsDrawer.tsx` |
| Per-key activity rendering | `ActivityMetrics` + `processActivityData(...,"api_keys")` | `components/activity_metrics.tsx` |
| Logs fetch (for drilldown) | `fetchLogs`/`UiSpendLogsParams` | `components/networking.tsx` |
| Per-model perf hook | `useModelPerformance` | `app/(dashboard)/hooks/models/useModelPerformance.ts` |
| Entity-scoped usage | `EntityUsage` + `ENTITY_FETCH_FNS` | `app/(dashboard)/usage/_components/EntityUsage/EntityUsage.tsx` |
| Chart loader placeholder | `ChartLoader` | `components/shared/chart_loader.tsx` |

What is missing (must be built):

1. **Un-gate the tab (section 0.1)**: render Model Performance in every usage
   view. Add a "Model Performance" tab to `EntityUsage` (and keep it in the
   global/my-usage tab list), and thread entity scope filters through
   `ModelPerformanceView` -> `useModelPerformance` -> `modelPerformanceCall`.
2. Entity scoping on the `/model/performance` backend query (a
   `team_id`/`user_id`/`end_user`/`api_key`/`customer_id` filter) so each usage
   view returns only its own entity's data and never leaks cross-entity data.
3. Real concurrent-request data (fix DB hardcode + Prometheus label mismatch).
4. A multi-select (checkbox) model filter in `ModelPerformanceView`.
5. Distinct color mapping + hover tooltip/line highlight (migrate to shared
   `LineChart`).
6. A reusable concurrent→logs drilldown drawer.

---

## 7. Verification notes for later

- After changes, follow `FRONTEND-ANALYSIS-PROCESS.md` and
  `UI-LINT-AND-CHANGE-PROCESS.md`: `npx tsc --noEmit`, `npx eslint ...`,
  `make pre-commit`, and live-browser verify.
- The gating extractor exists: `ui/litellm-dashboard/scripts/extract-gating.mjs`
  — use it to confirm where `ModelPerformanceView` renders after adding it to
  `EntityUsage`.
- Tests: no `ModelPerformanceView.test.tsx` exists yet; a new feature should
  add meaningful tests (see repo `CLAUDE.md` guidance).