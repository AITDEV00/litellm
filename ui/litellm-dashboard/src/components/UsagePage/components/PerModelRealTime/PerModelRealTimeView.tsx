import { useQuery } from "@tanstack/react-query";
import { Alert, Card, Select, Spin } from "antd";
import React, { useMemo, useState } from "react";

import {
  perModelMetricsCall,
  type PerModelDeploymentMetrics,
  type PerModelMetricsResponse,
  type PerModelTimeSeriesPoint,
} from "../../../networking";

interface PerModelRealTimeViewProps {
  accessToken: string | null;
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
  if (value < 1) return `${(value * 1000).toFixed(0)}ms`;
  return `${value.toFixed(3)}s`;
};

const latestValue = (points: PerModelTimeSeriesPoint[]): number =>
  points.length > 0 ? points[points.length - 1].value : 0;

const DeploymentCard: React.FC<{
  deployment: PerModelDeploymentMetrics;
  windowLabel: string;
}> = ({ deployment, windowLabel }) => {
  const latestConcurrent = latestValue(deployment.concurrent_requests);
  const latestRate = latestValue(deployment.request_rate);
  const latestTokens = latestValue(deployment.output_tokens_per_sec);
  const latestLatency = latestValue(deployment.latency_per_token_p50);

  return (
    <Card title={deployment.litellm_model_name || deployment.model_id} size="small">
      <p className="text-sm text-gray-500 mb-3">
        {deployment.api_provider} | {deployment.api_base}
      </p>

      <div className="grid grid-cols-2 gap-3">
        <div className="bg-gray-50 rounded-lg p-3">
          <p className="text-xs font-medium text-gray-500 mb-1">Concurrent Now</p>
          <p className="text-lg font-semibold text-cyan-600">{latestConcurrent}</p>
        </div>
        <div className="bg-gray-50 rounded-lg p-3">
          <p className="text-xs font-medium text-gray-500 mb-1">Request Rate ({windowLabel})</p>
          <p className="text-lg font-semibold">{formatRate(latestRate)}</p>
        </div>
        <div className="bg-gray-50 rounded-lg p-3">
          <p className="text-xs font-medium text-gray-500 mb-1">Output Tokens/sec ({windowLabel})</p>
          <p className="text-lg font-semibold">{formatTokens(latestTokens)}</p>
        </div>
        <div className="bg-gray-50 rounded-lg p-3">
          <p className="text-xs font-medium text-gray-500 mb-1">Latency/Token p50 ({windowLabel})</p>
          <p className="text-lg font-semibold">{formatLatency(latestLatency)}</p>
        </div>
      </div>

      <div className="bg-gray-50 rounded-lg p-3 mt-3">
        <p className="text-xs font-medium text-gray-500 mb-1">RPM Limit</p>
        <p className="text-lg font-semibold">{deployment.rpm_limit || "N/A"}</p>
      </div>

      <details className="mt-3">
        <summary className="text-sm text-gray-600 cursor-pointer hover:text-gray-800">
          Show time-series data points
        </summary>
        <div className="mt-2 space-y-2">
          {deployment.concurrent_requests.length > 0 && (
            <div>
              <p className="text-xs font-medium text-gray-400">Concurrent Requests (last 10)</p>
              <pre className="text-xs bg-gray-50 rounded p-2 overflow-x-auto">
                {JSON.stringify(deployment.concurrent_requests.slice(-10), null, 2)}
              </pre>
            </div>
          )}
          {deployment.request_rate.length > 0 && (
            <div>
              <p className="text-xs font-medium text-gray-400">Request Rate (last 10)</p>
              <pre className="text-xs bg-gray-50 rounded p-2 overflow-x-auto">
                {JSON.stringify(deployment.request_rate.slice(-10), null, 2)}
              </pre>
            </div>
          )}
          {deployment.output_tokens_per_sec.length > 0 && (
            <div>
              <p className="text-xs font-medium text-gray-400">Output Tokens/sec (last 10)</p>
              <pre className="text-xs bg-gray-50 rounded p-2 overflow-x-auto">
                {JSON.stringify(deployment.output_tokens_per_sec.slice(-10), null, 2)}
              </pre>
            </div>
          )}
          {deployment.latency_per_token_p50.length > 0 && (
            <div>
              <p className="text-xs font-medium text-gray-400">Latency/Token p50 (last 10)</p>
              <pre className="text-xs bg-gray-50 rounded p-2 overflow-x-auto">
                {JSON.stringify(deployment.latency_per_token_p50.slice(-10), null, 2)}
              </pre>
            </div>
          )}
        </div>
      </details>
    </Card>
  );
};

const PerModelRealTimeView: React.FC<PerModelRealTimeViewProps> = ({ accessToken }) => {
  const [window, setWindow] = useState("1h");
  const [modelId, setModelId] = useState<string | undefined>(undefined);

  const query = useQuery<PerModelMetricsResponse>({
    queryKey: ["perModelMetrics", window, modelId],
    queryFn: () => perModelMetricsCall(accessToken!, { window, model_id: modelId }),
    enabled: Boolean(accessToken),
    refetchInterval: 15_000,
  });

  const deployments = useMemo(() => query.data?.deployments ?? [], [query.data]);
  const windowLabel = WINDOWS.find((w) => w.value === window)?.label ?? window;

  const modelIdOptions = useMemo(() => {
    const seen = new Map<string, string>();
    for (const d of deployments) {
      const label = d.litellm_model_name || d.model_id;
      seen.set(d.model_id, label);
    }
    return Array.from(seen.entries()).map(([id, label]) => ({ label, value: id }));
  }, [deployments]);

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <span className="text-sm font-medium text-gray-700">Time window:</span>
        <Select
          style={{ width: 200 }}
          value={window}
          onChange={(value: string) => setWindow(value)}
          options={WINDOWS}
        />
        <span className="text-sm font-medium text-gray-700 ml-4">Deployment:</span>
        <Select
          style={{ width: 300 }}
          value={modelId ?? "all"}
          onChange={(value: string) => setModelId(value === "all" ? undefined : value)}
          placeholder="All deployments"
          options={[{ label: "All deployments", value: "all" }, ...modelIdOptions]}
          showSearch
        />
      </div>

      {query.data && !query.data.prometheus_connected && (
        <Alert
          message="Prometheus is not connected. Showing only the current in-progress request count (no historical time-series)."
          type="info"
          showIcon
        />
      )}

      {query.isLoading && (
        <div className="flex justify-center py-12">
          <Spin size="large" />
        </div>
      )}

      {query.isError && <Alert message="Failed to load per-model metrics." type="error" showIcon />}

      {!query.isLoading && !query.isError && deployments.length === 0 && (
        <Card>
          <p className="text-gray-500 text-sm py-8 text-center">No deployment metrics found for the selected window.</p>
        </Card>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {deployments.map((d) => (
          <DeploymentCard key={d.model_id} deployment={d} windowLabel={windowLabel} />
        ))}
      </div>
    </div>
  );
};

export default PerModelRealTimeView;
