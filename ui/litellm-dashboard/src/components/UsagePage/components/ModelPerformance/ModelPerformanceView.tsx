import { Card, Grid, Table, TableBody, TableCell, TableHead, TableHeaderCell, TableRow, Title } from "@tremor/react";
import { Segmented, Select } from "antd";
import React, { memo, useCallback, useDeferredValue, useEffect, useMemo, useState } from "react";
import { useDebouncedValue } from "@tanstack/react-pacer/debouncer";

import { useModelPerformance } from "@/app/(dashboard)/hooks/models/useModelPerformance";
import { ChartLoader } from "../../../shared/chart_loader";
import { LineChart, DEFAULT_COLOR_CYCLE } from "../../../shared/charts";
import { UiLoadingSpinner } from "../../../ui/ui-loading-spinner";
import type { ModelPerformanceModel, ModelPerformanceScope, ModelPerformanceTimePoint } from "../../../networking";
import { uiSpendLogsCall } from "../../../networking";
import { LogDetailsDrawer } from "../../../view_logs/LogDetailsDrawer/LogDetailsDrawer";
import type { LogEntry } from "../../../view_logs/columns";

const WINDOW_OPTIONS = [
  { label: "5m", value: "5m" },
  { label: "15m", value: "15m" },
  { label: "1h", value: "1h" },
  { label: "24h", value: "24h" },
  { label: "7d", value: "7d" },
];

// When "Live" is active the component overrides the shared date range and the
// internal window with a short Prometheus-backed window that auto-refreshes,
// so the tab shows near-real-time concurrent requests / throughput regardless
// of which date period the user selected in the top "Select Time Range" widget.
const LIVE_WINDOW = "5m";
const LIVE_REFRESH_MS = 10_000;

// x-axis granularity knob ("zoom in/out"): smaller buckets = smoother curves
// with more points; larger buckets = coarser. DB-backed windows (24h/7d) map
// each option to a Postgres interval; Prometheus-backed windows (5m/15m/1h)
// are not zoomable, so the knob is hidden for them. The "" value means "use
// the backend's per-window default bucket" (24h=1h, 7d=6h) so the knob is
// safe by default and users can zoom in (e.g. 7d at 1 hour) for smooth curves.
const GRANULARITY_OPTIONS: Record<string, { label: string; value: string }[]> = {
  "24h": [
    { label: "Auto", value: "" },
    { label: "30m", value: "30 minutes" },
    { label: "1h", value: "1 hour" },
    { label: "2h", value: "2 hours" },
    { label: "6h", value: "6 hours" },
  ],
  "7d": [
    { label: "Auto", value: "" },
    { label: "1h", value: "1 hour" },
    { label: "3h", value: "3 hours" },
    { label: "6h", value: "6 hours" },
    { label: "12h", value: "12 hours" },
  ],
};

// Granularity options for a custom time range (the shared "Select Time Range"
// widget), bucketed by the range duration. "" = backend-computed default.
function getRangeGranularityOptions(durationMs: number): { label: string; value: string }[] {
  const hours = durationMs / (60 * 60 * 1000);
  if (hours <= 2) {
    return [
      { label: "Auto", value: "" },
      { label: "5m", value: "5 minutes" },
      { label: "15m", value: "15 minutes" },
      { label: "30m", value: "30 minutes" },
    ];
  }
  if (hours <= 24) {
    return [
      { label: "Auto", value: "" },
      { label: "30m", value: "30 minutes" },
      { label: "1h", value: "1 hour" },
      { label: "2h", value: "2 hours" },
      { label: "6h", value: "6 hours" },
    ];
  }
  if (hours <= 72) {
    return [
      { label: "Auto", value: "" },
      { label: "1h", value: "1 hour" },
      { label: "3h", value: "3 hours" },
      { label: "6h", value: "6 hours" },
      { label: "12h", value: "12 hours" },
    ];
  }
  return [
    { label: "Auto", value: "" },
    { label: "6h", value: "6 hours" },
    { label: "12h", value: "12 hours" },
    { label: "1d", value: "1 day" },
    { label: "3d", value: "3 days" },
  ];
}

// Map a time range to the closest predefined window (used only for the query
// refetch cadence and cache key; the backend honors start_time/end_time).
function deriveWindowFromRange(durationMs: number): string {
  const h = durationMs / (60 * 60 * 1000);
  if (h <= 0.25) return "5m";
  if (h <= 1) return "15m";
  if (h <= 6) return "1h";
  if (h <= 48) return "24h";
  return "7d";
}

function transformTimeSeries(
  models: ModelPerformanceModel[],
  seriesKey: "concurrent_requests" | "throughput_tokens_per_sec" | "ttft_seconds",
): { data: Array<Record<string, string | number | null>>; categories: string[] } {
  if (!models || models.length === 0) {
    return { data: [], categories: [] };
  }

  const modelGroups = models.map((m) => m.model_group);
  const allTimestamps = new Set<string>();
  const lookup: Record<string, Record<string, number | null>> = {};

  for (const model of models) {
    const series: ModelPerformanceTimePoint[] = model.time_series[seriesKey] || [];
    for (const point of series) {
      if (!point.timestamp) continue;
      allTimestamps.add(point.timestamp);
      if (!lookup[point.timestamp]) {
        lookup[point.timestamp] = {};
      }
      lookup[point.timestamp][model.model_group] = point.value;
    }
  }

  const sortedTimestamps = Array.from(allTimestamps).sort();
  const data = sortedTimestamps.map((ts) => {
    const row: Record<string, string | number | null> = {
      timestamp: _formatTimestamp(ts),
    };
    for (const mg of modelGroups) {
      row[mg] = lookup[ts]?.[mg] ?? null;
    }
    return row;
  });

  return { data, categories: modelGroups };
}

function _formatTimestamp(ts: string): string {
  try {
    const dt = new Date(ts);
    return dt.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" });
  } catch {
    return ts;
  }
}

function formatNumber(value: number | null | undefined, decimals: number = 2): string {
  if (value === null || value === undefined || isNaN(value)) return "—";
  return value.toFixed(decimals);
}

interface PerformanceChartProps {
  title: string;
  data: Array<Record<string, string | number | null>>;
  categories: string[];
  decimals: number;
  onPointClick?: (datum: Record<string, string | number | null>, category: string) => void;
}

const PerformanceChart = memo(function PerformanceChart({
  title,
  data,
  categories,
  decimals,
  onPointClick,
}: PerformanceChartProps) {
  if (data.length === 0) {
    return (
      <Card>
        <Title>{title}</Title>
        <p className="text-gray-500 mt-4">No data available</p>
      </Card>
    );
  }
  return (
    <Card>
      <Title>{title}</Title>
      <LineChart
        className="mt-4"
        data={data}
        index="timestamp"
        categories={categories}
        colors={DEFAULT_COLOR_CYCLE}
        valueFormatter={(v) => formatNumber(v, decimals)}
        connectNulls
        yAxisWidth={60}
        onPointClick={onPointClick}
      />
    </Card>
  );
});

interface ModelPerformanceViewProps {
  scope?: ModelPerformanceScope;
  accessToken?: string | null;
  /** Optional time range from the shared "Select Time Range" widget. When
   * present, it overrides the internal window-relative date. */
  dateValue?: { from?: Date | null; to?: Date | null };
}

const ModelPerformanceView: React.FC<ModelPerformanceViewProps> = ({ scope = {}, accessToken, dateValue }) => {
  const [window, setWindow] = useState<string>("1h");
  const [granularity, setGranularity] = useState<string>("");
  const [live, setLive] = useState<boolean>(false);
  const [selectedModelGroups, setSelectedModelGroups] = useState<string[]>([]);
  const [selectedLog, setSelectedLog] = useState<LogEntry | null>(null);
  const [drilldownLogs, setDrilldownLogs] = useState<LogEntry[]>([]);
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);

  const [debouncedWindow] = useDebouncedValue(window, { wait: 200 });
  const [debouncedGranularity] = useDebouncedValue(granularity, { wait: 200 });

  // Live mode is a self-contained realtime view: it overrides the shared date
  // range widget AND the internal window with a short Prometheus-backed window
  // that auto-refreshes. It is disabled whenever the view is entity-scoped
  // (team/user/key/etc.) because the Prometheus live metrics do not carry
  // entity-scope labels, so per-entity realtime would leak cross-entity data.
  const canLive = !(
    scope.teamId ||
    scope.organizationId ||
    scope.userId ||
    scope.endUserId ||
    scope.apiKey ||
    scope.agentId
  );

  // The parent "Select Time Range" widget (if provided) owns the date range;
  // we pass its from/to through to the backend, which overrides the
  // window-relative start. The internal window segmented control is hidden in
  // that case, but the granularity knob stays so users can still zoom.
  const hasCustomRange = Boolean(dateValue?.from && dateValue?.to);
  const rangeDurationMs = useMemo(() => {
    if (!dateValue?.from || !dateValue?.to) return 0;
    return Math.max(0, dateValue.to.getTime() - dateValue.from.getTime());
  }, [dateValue?.from, dateValue?.to]);

  // When a custom range is active, derive the window from its duration (used
  // for refetch cadence / cache key) and reset granularity if the range size
  // class changes so the option set always fits the range. Live mode overrides
  // everything and forces the short realtime window.
  const effectiveWindow = live ? LIVE_WINDOW : hasCustomRange ? deriveWindowFromRange(rangeDurationMs) : debouncedWindow;
  const granularityOptions = live
    ? []
    : hasCustomRange
      ? getRangeGranularityOptions(rangeDurationMs)
      : (GRANULARITY_OPTIONS[effectiveWindow] ?? []);

  useEffect(() => {
    if (!live && hasCustomRange) setGranularity("");
  }, [live, hasCustomRange, rangeDurationMs]);

  // When toggling into/out of live mode, reset granularity so stale zoom levels
  // don't carry across modes.
  useEffect(() => {
    setGranularity("");
  }, [live]);

  const effectiveScope = useMemo<ModelPerformanceScope>(() => {
    // Live mode must ignore the shared date range entirely.
    if (live) return scope;
    if (!hasCustomRange) return scope;
    return {
      ...scope,
      startTime: dateValue!.from!.toISOString(),
      endTime: dateValue!.to!.toISOString(),
    };
  }, [scope, live, hasCustomRange, dateValue?.from, dateValue?.to]);

  const { data, isLoading, isFetching, isError, dataUpdatedAt } = useModelPerformance(
    effectiveWindow,
    undefined,
    effectiveScope,
    debouncedGranularity,
    live,
  );

  const models = useMemo(() => data?.models || [], [data]);
  const deferredModels = useDeferredValue(models);

  const filteredModels = useMemo(() => {
    if (selectedModelGroups.length === 0) return deferredModels;
    const selected = new Set(selectedModelGroups);
    return deferredModels.filter((m) => selected.has(m.model_group));
  }, [deferredModels, selectedModelGroups]);

  const modelGroupOptions = useMemo(() => {
    return models.map((m) => ({ label: m.model_group, value: m.model_group }));
  }, [models]);

  const concurrentChart = useMemo(() => transformTimeSeries(filteredModels, "concurrent_requests"), [filteredModels]);
  const throughputChart = useMemo(
    () => transformTimeSeries(filteredModels, "throughput_tokens_per_sec"),
    [filteredModels],
  );
  const ttftChart = useMemo(() => transformTimeSeries(filteredModels, "ttft_seconds"), [filteredModels]);

  const isStale = deferredModels !== models || isFetching;

  const handleConcurrentPointClick = useCallback(
    (datum: Record<string, string | number | null>, category: string) => {
      const timestamp = typeof datum.timestamp === "string" ? datum.timestamp : undefined;
      if (!accessToken || !timestamp) return;
      void (async () => {
        try {
          const end = new Date(timestamp);
          const start = new Date(end.getTime() - 5 * 60 * 1000);
          const logOptions = {
            accessToken,
            start_date: start.toISOString(),
            end_date: end.toISOString(),
            page: 1,
            page_size: 50,
            params: {
              model: category,
              ...scopeToParams(scope),
            },
          };
          const result = await uiSpendLogsCall(logOptions);
          const logs = (result as { data?: LogEntry[] }).data || [];
          setDrilldownLogs(logs);
          setSelectedLog(logs[0] || null);
          setIsDrawerOpen(true);
        } catch (error) {
          console.error("Failed to fetch concurrent drilldown logs:", error);
        }
      })();
    },
    [accessToken, scope],
  );

  const handleCloseDrawer = useCallback(() => {
    setIsDrawerOpen(false);
    setSelectedLog(null);
    setDrilldownLogs([]);
  }, []);

  const renderContent = (): React.ReactNode => {
    if (isError) {
      return (
        <Card>
          <Title>Failed to load performance data</Title>
        </Card>
      );
    }
    // Initial load (no cached data yet) or an in-flight refetch after the user
    // changed the window / granularity / model selection.
    if (isLoading || (isFetching && models.length === 0)) {
      return (
        <Card>
          <ChartLoader />
        </Card>
      );
    }
    if (models.length === 0) {
      return (
        <Card>
          <Title>No performance data for the selected window</Title>
        </Card>
      );
    }
    return (
      <>
        <Grid numItems={1} className="gap-4">
          {isFetching && models.length > 0 && (
            <div className="flex items-center justify-center gap-2 text-xs text-gray-400">
              <UiLoadingSpinner className="size-4" />
              <span>Refreshing charts…</span>
            </div>
          )}
          <PerformanceChart
            title="Concurrent Requests"
            data={concurrentChart.data}
            categories={concurrentChart.categories}
            decimals={0}
            onPointClick={handleConcurrentPointClick}
          />

          <PerformanceChart
            title="Throughput (tokens/sec)"
            data={throughputChart.data}
            categories={throughputChart.categories}
            decimals={1}
          />

          <PerformanceChart
            title="Time to First Token (seconds)"
            data={ttftChart.data}
            categories={ttftChart.categories}
            decimals={3}
          />
        </Grid>

        <Card>
          <Title>Summary</Title>
          <Table className="mt-4">
            <TableHead>
              <TableRow>
                <TableHeaderCell>Model Group</TableHeaderCell>
                <TableHeaderCell>Avg Concurrent</TableHeaderCell>
                <TableHeaderCell>Avg Throughput (tok/s)</TableHeaderCell>
                <TableHeaderCell>P50 TTFT (s)</TableHeaderCell>
                <TableHeaderCell>P95 TTFT (s)</TableHeaderCell>
                <TableHeaderCell>Total Requests</TableHeaderCell>
                <TableHeaderCell>Total Tokens</TableHeaderCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {filteredModels.map((m) => (
                <TableRow key={m.model_group}>
                  <TableCell>{m.model_group}</TableCell>
                  <TableCell>{formatNumber(m.summary.avg_concurrent, 1)}</TableCell>
                  <TableCell>{formatNumber(m.summary.avg_throughput, 1)}</TableCell>
                  <TableCell>{formatNumber(m.summary.p50_ttft, 3)}</TableCell>
                  <TableCell>{formatNumber(m.summary.p95_ttft, 3)}</TableCell>
                  <TableCell>{m.summary.total_requests.toLocaleString()}</TableCell>
                  <TableCell>{m.summary.total_tokens.toLocaleString()}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </Card>
      </>
    );
  };

  return (
    <div className="space-y-4" style={{ opacity: isStale ? 0.7 : 1, transition: "opacity 0.2s" }}>
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          {canLive && (
            <Segmented
              options={[
                { label: "Live", value: "live" },
                { label: "Historical", value: "hist" },
              ]}
              value={live ? "live" : "hist"}
              onChange={(val) => setLive(val === "live")}
            />
          )}
          {!live && !hasCustomRange && (
            <Segmented options={WINDOW_OPTIONS} value={window} onChange={(val) => {
              setWindow(val as string);
              setGranularity("");
            }} />
          )}
          {!live && (
            <Segmented
              size="small"
              options={granularityOptions}
              value={granularity}
              onChange={(val) => setGranularity(val as string)}
            />
          )}
          {live && dataUpdatedAt > 0 && (
            <span className="text-xs text-gray-500 whitespace-nowrap">
              Updated {new Date(dataUpdatedAt).toLocaleTimeString()}
              {isFetching && " · refreshing"}
            </span>
          )}
        </div>
        <Select
          style={{ minWidth: 280, maxWidth: 380 }}
          mode="multiple"
          placeholder="Search and select model groups"
          value={selectedModelGroups}
          onChange={(vals: string[]) => setSelectedModelGroups(vals)}
          options={modelGroupOptions}
          allowClear
          showSearch
          maxTagCount="responsive"
          optionFilterProp="label"
        />
      </div>

      {renderContent()}

      <LogDetailsDrawer
        open={isDrawerOpen}
        onClose={handleCloseDrawer}
        logEntry={selectedLog}
        accessToken={accessToken}
        allLogs={drilldownLogs}
        onSelectLog={setSelectedLog}
      />
    </div>
  );
};

function scopeToParams(scope: ModelPerformanceScope): Record<string, string> {
  const params: Record<string, string> = {};
  if (scope.teamId) params.team_id = scope.teamId;
  if (scope.organizationId) params.organization_id = scope.organizationId;
  if (scope.userId) params.user_id = scope.userId;
  if (scope.endUserId) params.end_user = scope.endUserId;
  if (scope.apiKey) params.api_key = scope.apiKey;
  if (scope.agentId) params.agent_id = scope.agentId;
  return params;
}

export default ModelPerformanceView;
