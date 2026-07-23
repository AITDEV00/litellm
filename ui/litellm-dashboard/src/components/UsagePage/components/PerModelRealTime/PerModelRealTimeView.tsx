import { useQuery } from "@tanstack/react-query";
import { AreaChart } from "@tremor/react";
import { Alert, Card, Select, Spin } from "antd";
import React, { useMemo, useState } from "react";

import {
  modelInfoCall,
  perModelMetricsCall,
  type PerModelDeploymentMetrics,
  type PerModelMetricsResponse,
  type PerModelTimeSeriesPoint,
} from "../../../networking";

interface PerModelRealTimeViewProps {
  accessToken: string | null;
  userID: string | null;
  userRole: string | null;
}

const WINDOWS = [
  { label: "Last 1 minute", value: "1m" },
  { label: "Last 15 minutes", value: "15m" },
  { label: "Last 1 hour", value: "1h" },
  { label: "Last 24 hours", value: "24h" },
  { label: "Last 7 days", value: "7d" },
];

const formatRate = (value: number) => `${value.toFixed(2)}/s`;
const formatTokens = (value: number) => `${value.toFixed(1)} tok/s`;
const formatLatency = (value: number) => {
  if (value < 0.001) return "0ms";
  if (value < 1) return `${(value * 1000).toFixed(0)}ms`;
  return `${value.toFixed(3)}s`;
};
const formatConcurrent = (value: number) => String(Math.round(value));

const REFRESH_INTERVAL_MS = 15_000;

const latestValue = (points: PerModelTimeSeriesPoint[]): number =>
  points.length > 0 ? points[points.length - 1].value : 0;

interface SeriesConfigEntry {
  key: "concurrent_requests" | "request_rate" | "output_tokens_per_sec" | "latency_per_token_p50";
  label: string;
  formatter: (v: number) => string;
  color: string;
}

const SERIES_CONFIG: SeriesConfigEntry[] = [
  { key: "concurrent_requests", label: "Concurrent Requests", formatter: formatConcurrent, color: "cyan" },
  { key: "request_rate", label: "Request Rate", formatter: formatRate, color: "blue" },
  { key: "output_tokens_per_sec", label: "Output Tokens/sec", formatter: formatTokens, color: "emerald" },
  { key: "latency_per_token_p50", label: "Latency/Token p50", formatter: formatLatency, color: "amber" },
];

const toChartData = (points: PerModelTimeSeriesPoint[]) =>
  points.map((p) => ({
    timestamp: new Date(p.timestamp).toLocaleTimeString(),
    value: p.value,
  }));

const DeploymentCard: React.FC<{
  deployment: PerModelDeploymentMetrics;
  windowLabel: string;
}> = ({ deployment, windowLabel }) => {
  return (
    <Card>
      <div className="flex items-center justify-between mb-1">
        <h3 className="text-lg font-semibold text-gray-900">{deployment.litellm_model_name || deployment.model_id}</h3>
        {deployment.rpm_limit > 0 && <span className="text-xs text-gray-500">RPM limit: {deployment.rpm_limit}</span>}
      </div>
      <p className="text-sm text-gray-500 mb-4">
        {deployment.api_provider}
        {deployment.api_base ? ` | ${deployment.api_base}` : ""}
      </p>

      <div className="space-y-4">
        {SERIES_CONFIG.map(({ key, label, formatter, color }) => {
          const points = deployment[key];
          const latest = latestValue(points);
          const chartData = toChartData(points);
          return (
            <div key={key} data-testid={`series-${key}`}>
              <div className="flex items-baseline justify-between mb-1">
                <span className="text-sm font-medium text-gray-700">{label}</span>
                <span className="text-sm font-semibold text-gray-900" data-testid={`latest-${key}`}>
                  {formatter(latest)}
                </span>
              </div>
              {chartData.length > 1 ? (
                <AreaChart
                  className="h-12"
                  data={chartData}
                  index="timestamp"
                  categories={["value"]}
                  colors={[color]}
                  valueFormatter={formatter}
                  showLegend={false}
                  showXAxis={true}
                  showYAxis={true}
                />
              ) : (
                <div
                  className="h-12 flex items-center justify-center text-xs text-gray-400 bg-gray-50 rounded"
                  data-testid={`no-data-${key}`}
                >
                  No data for {windowLabel.toLowerCase()}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
};

interface RegisteredModel {
  modelId: string;
  modelName: string;
  litellmModel: string;
}

interface ModelInfoEntry {
  model_name: string;
  litellm_params: { model?: string };
  model_info: { id?: string };
}

interface ModelInfoResponse {
  data: ModelInfoEntry[];
}

const PerModelRealTimeView: React.FC<PerModelRealTimeViewProps> = ({ accessToken, userID, userRole }) => {
  const [timeWindow, setTimeWindow] = useState("1h");
  const [selectedModelId, setSelectedModelId] = useState<string | undefined>(undefined);

  const modelsQuery = useQuery({
    queryKey: ["perModelRegisteredModels", accessToken],
    queryFn: async () => {
      return await modelInfoCall(accessToken!, userID || "default", userRole || "proxy_admin", 1, 100);
    },
    enabled: Boolean(accessToken),
  });

  const registeredModels: RegisteredModel[] = useMemo(() => {
    const data = (modelsQuery.data as ModelInfoResponse | undefined)?.data ?? [];
    return data.map((m) => ({
      modelId: m.model_info?.id ?? "",
      modelName: m.model_name ?? "",
      litellmModel: m.litellm_params?.model ?? "",
    }));
  }, [modelsQuery.data]);

  const modelIdOptions = useMemo(() => {
    const options = registeredModels.map((m) => ({
      label: m.modelName || m.litellmModel || m.modelId,
      value: m.modelId,
    }));
    return [{ label: "All registered models", value: "all" }, ...options];
  }, [registeredModels]);

  const metricsQuery = useQuery<PerModelMetricsResponse>({
    queryKey: ["perModelMetrics", timeWindow, selectedModelId],
    queryFn: () => perModelMetricsCall(accessToken!, { window: timeWindow, model_id: selectedModelId }),
    enabled: Boolean(accessToken),
    refetchInterval: REFRESH_INTERVAL_MS,
  });

  const deployments = useMemo(() => metricsQuery.data?.deployments ?? [], [metricsQuery.data]);
  const windowLabel = WINDOWS.find((w) => w.value === timeWindow)?.label ?? timeWindow;

  const deploymentsWithNames = useMemo(() => {
    const modelMap = new Map(registeredModels.map((m) => [m.modelId, m]));
    return deployments.map((d) => {
      const registered = modelMap.get(d.model_id);
      if (registered && (registered.modelName || registered.litellmModel) && !d.litellm_model_name) {
        return { ...d, litellm_model_name: registered.litellmModel || registered.modelName };
      }
      return d;
    });
  }, [deployments, registeredModels]);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4 flex-wrap">
        <span className="text-sm font-medium text-gray-700">Time window:</span>
        <Select
          style={{ width: 200 }}
          value={timeWindow}
          onChange={(value: string) => setTimeWindow(value)}
          options={WINDOWS}
        />
        <span className="text-sm font-medium text-gray-700 ml-4">Model:</span>
        <Select
          style={{ width: 350 }}
          value={selectedModelId ?? "all"}
          onChange={(value: string) => setSelectedModelId(value === "all" ? undefined : value)}
          placeholder="All registered models"
          options={modelIdOptions}
          showSearch
          loading={modelsQuery.isLoading}
        />
      </div>

      {metricsQuery.data && !metricsQuery.data.prometheus_connected && (
        <Alert
          message="Prometheus is not connected. Showing only the current in-progress request count (no historical time-series)."
          type="info"
          showIcon
        />
      )}

      {metricsQuery.isLoading && (
        <div className="flex justify-center py-12">
          <Spin size="large" />
        </div>
      )}

      {metricsQuery.isError && <Alert message="Failed to load per-model metrics." type="error" showIcon />}

      {!metricsQuery.isLoading && !metricsQuery.isError && deploymentsWithNames.length === 0 && (
        <Card>
          <p className="text-gray-500 text-sm py-8 text-center">No deployment metrics found for the selected window.</p>
        </Card>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {deploymentsWithNames.map((d) => (
          <DeploymentCard key={d.model_id} deployment={d} windowLabel={windowLabel} />
        ))}
      </div>
    </div>
  );
};

export default PerModelRealTimeView;
