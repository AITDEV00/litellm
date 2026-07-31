import {
  Card,
  Grid,
  LineChart,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
  Title,
} from "@tremor/react";
import { Segmented, Select } from "antd";
import React, { memo, useDeferredValue, useEffect, useMemo, useState } from "react";

import { useModelPerformance } from "@/app/(dashboard)/hooks/models/useModelPerformance";
import { ChartLoader } from "../../../shared/chart_loader";
import type { ModelPerformanceModel, ModelPerformanceTimePoint } from "../../../networking";

const WINDOW_OPTIONS = [
  { label: "5m", value: "5m" },
  { label: "15m", value: "15m" },
  { label: "1h", value: "1h" },
  { label: "24h", value: "24h" },
  { label: "7d", value: "7d" },
];

const TREMOR_COLORS = ["blue", "cyan", "indigo", "violet", "purple", "fuchsia", "pink", "rose", "red", "orange"];

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
}

const PerformanceChart = memo(function PerformanceChart({ title, data, categories, decimals }: PerformanceChartProps) {
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
        className="h-72 mt-4"
        data={data}
        index="timestamp"
        categories={categories}
        colors={TREMOR_COLORS.slice(0, categories.length)}
        valueFormatter={(v) => formatNumber(v, decimals)}
        showLegend={categories.length <= 8}
        connectNulls
        yAxisWidth={60}
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

const ModelPerformanceView: React.FC = () => {
  const [window, setWindow] = useState<string>("1h");
  const [selectedModelGroup, setSelectedModelGroup] = useState<string | undefined>(undefined);

  const debouncedWindow = useDebouncedValue(window, 200);
  const { data, isLoading, isError } = useModelPerformance(debouncedWindow, selectedModelGroup);

  const models = useMemo(() => data?.models || [], [data]);
  const deferredModels = useDeferredValue(models);

  const modelGroupOptions = useMemo(() => {
    const opts = [{ label: "All Models", value: "" }];
    for (const m of models) {
      opts.push({ label: m.model_group, value: m.model_group });
    }
    return opts;
  }, [models]);

  const concurrentChart = useMemo(() => transformTimeSeries(deferredModels, "concurrent_requests"), [deferredModels]);
  const throughputChart = useMemo(
    () => transformTimeSeries(deferredModels, "throughput_tokens_per_sec"),
    [deferredModels],
  );
  const ttftChart = useMemo(() => transformTimeSeries(deferredModels, "ttft_seconds"), [deferredModels]);

  const isStale = deferredModels !== models;

  return (
    <div className="space-y-4" style={{ opacity: isStale ? 0.7 : 1, transition: "opacity 0.2s" }}>
      <div className="flex items-center justify-between gap-4">
        <Segmented options={WINDOW_OPTIONS} value={window} onChange={(val) => setWindow(val as string)} />
        <Select
          style={{ width: 220 }}
          placeholder="Select model group"
          value={selectedModelGroup || ""}
          onChange={(val: string) => setSelectedModelGroup(val || undefined)}
          options={modelGroupOptions}
          allowClear
        />
      </div>

      {isError && (
        <Card>
          <Title>Failed to load performance data</Title>
        </Card>
      )}

      {isLoading ? (
        <Card>
          <ChartLoader />
        </Card>
      ) : models.length === 0 ? (
        <Card>
          <Title>No performance data for the selected window</Title>
        </Card>
      ) : (
        <>
          <Grid numItems={1} className="gap-4">
            {isStale && <p className="text-xs text-gray-400">Updating charts…</p>}
            <PerformanceChart
              title="Concurrent Requests"
              data={concurrentChart.data}
              categories={concurrentChart.categories}
              decimals={0}
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
                {models.map((m) => (
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
      )}
    </div>
  );
};

export default ModelPerformanceView;
