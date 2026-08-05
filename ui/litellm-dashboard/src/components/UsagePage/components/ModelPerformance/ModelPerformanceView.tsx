import { Card, Grid, Table, TableBody, TableCell, TableHead, TableHeaderCell, TableRow, Title } from "@tremor/react";
import { Segmented, Select } from "antd";
import React, { memo, useCallback, useDeferredValue, useEffect, useMemo, useState } from "react";

import { useModelPerformance } from "@/app/(dashboard)/hooks/models/useModelPerformance";
import { ChartLoader } from "../../../shared/chart_loader";
import { LineChart, DEFAULT_COLOR_CYCLE } from "../../../shared/charts";
import type { ModelPerformanceModel, ModelPerformanceScope, ModelPerformanceTimePoint } from "../../../networking";
import { uiSpendLogsCall } from "../../../networking";
import { LogDetailsDrawer } from "../../view_logs/LogDetailsDrawer/LogDetailsDrawer";
import type { LogEntry } from "../../view_logs/columns";

const WINDOW_OPTIONS = [
  { label: "5m", value: "5m" },
  { label: "15m", value: "15m" },
  { label: "1h", value: "1h" },
  { label: "24h", value: "24h" },
  { label: "7d", value: "7d" },
];

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

function useDebouncedValue<T>(value: T, delay: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

interface ModelPerformanceViewProps {
  scope?: ModelPerformanceScope;
  accessToken?: string | null;
}

const ModelPerformanceView: React.FC<ModelPerformanceViewProps> = ({ scope = {}, accessToken }) => {
  const [window, setWindow] = useState<string>("1h");
  const [selectedModelGroups, setSelectedModelGroups] = useState<string[]>([]);
  const [selectedLog, setSelectedLog] = useState<LogEntry | null>(null);
  const [drilldownLogs, setDrilldownLogs] = useState<LogEntry[]>([]);
  const [isDrawerOpen, setIsDrawerOpen] = useState(false);

  const debouncedWindow = useDebouncedValue(window, 200);
  const { data, isLoading, isError } = useModelPerformance(debouncedWindow, undefined, scope);

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

  const isStale = deferredModels !== models;

  const handleConcurrentPointClick = useCallback(
    (datum: Record<string, string | number | null>, category: string) => {
      const timestamp = typeof datum.timestamp === "string" ? datum.timestamp : undefined;
      if (!accessToken || !timestamp) return;
      void (async () => {
        try {
          const end = new Date(timestamp);
          const start = new Date(end.getTime() - 5 * 60 * 1000);
          const result = await uiSpendLogsCall({
            accessToken,
            start_date: start.toISOString(),
            end_date: end.toISOString(),
            page: 1,
            page_size: 50,
            params: {
              model: category,
              ...scopeToParams(scope),
            },
          });
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
    if (isLoading) {
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
          {isStale && <p className="text-xs text-gray-400">Updating charts…</p>}
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
        <Segmented options={WINDOW_OPTIONS} value={window} onChange={(val) => setWindow(val as string)} />
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
