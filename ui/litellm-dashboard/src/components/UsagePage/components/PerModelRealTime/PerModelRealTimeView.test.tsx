import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tremor/react", async () => {
  const React = await import("react");
  const AreaChart = (props: any) =>
    React.createElement("div", { "data-testid": "tremor-area-chart" }, JSON.stringify(props.categories));
  (AreaChart as any).displayName = "AreaChart";
  return { AreaChart };
});

vi.mock("antd", async () => {
  const React = await import("react");
  const SelectComponent = ({ children, onChange, value, placeholder, loading, ...props }: any) => (
    <div data-testid="antd-select" data-value={value} data-loading={loading ? "true" : undefined}>
      <select data-testid="select-input" value={value || ""} onChange={(e) => onChange?.(e.target.value)}>
        {children}
      </select>
      {placeholder && <span data-testid="select-placeholder">{placeholder}</span>}
      {loading && <span data-testid="select-loading">Loading</span>}
    </div>
  );
  function SelectOption({ children, value, ...props }: any) {
    return (
      <option value={value} {...props}>
        {children}
      </option>
    );
  }
  SelectComponent.Option = SelectOption;
  (SelectComponent as any).displayName = "Select";
  const Card = ({ children, ...props }: any) => React.createElement("div", { "data-testid": "antd-card" }, children);
  (Card as any).displayName = "Card";
  const Alert = ({ message, ...props }: any) => React.createElement("div", { "data-testid": "antd-alert" }, message);
  (Alert as any).displayName = "Alert";
  const Spin = ({ size, ...props }: any) =>
    React.createElement("div", { "data-testid": "antd-spin", "data-size": size });
  (Spin as any).displayName = "Spin";
  return { Select: SelectComponent, Card, Alert, Spin };
});

vi.mock("../../../networking", () => ({
  modelInfoCall: vi.fn(),
  perModelMetricsCall: vi.fn(),
}));

import { modelInfoCall, perModelMetricsCall } from "../../../networking";
import type { PerModelMetricsResponse } from "../../../networking";
import PerModelRealTimeView from "./PerModelRealTimeView";

const mockModelInfo = {
  data: [
    {
      model_name: "zai-org/GLM-5.2-FP8",
      litellm_params: { model: "hosted_vllm/zai-org/GLM-5.2-FP8" },
      model_info: { id: "e2acef83-041b-4c43-a96d-d28f7ad2bef2" },
    },
    {
      model_name: "Qwen3.6-35B-A3B-FP8",
      litellm_params: { model: "hosted_vllm/Qwen3.6-35B-A3B-FP8" },
      model_info: { id: "323f6385-5061-4ada-ba17-4fb85c770813" },
    },
    {
      model_name: "Qwen/Qwen3-Embedding-4B",
      litellm_params: { model: "hosted_vllm/Qwen/Qwen3-Embedding-4B" },
      model_info: { id: "a7c23c69-a80a-474a-b9d1-d0448f1eff39" },
    },
  ],
  total_count: 3,
  current_page: 1,
  total_pages: 1,
  size: 100,
};

const makeTs = (count: number, baseValue: number = 1.0) =>
  Array.from({ length: count }, (_, i) => ({
    timestamp: new Date(1700000000000 + i * 30000).toISOString(),
    value: baseValue + i * 0.1,
  }));

const mockMetricsResponse: PerModelMetricsResponse = {
  prometheus_connected: true,
  window: "1h",
  step: "30s",
  deployments: [
    {
      model_id: "e2acef83-041b-4c43-a96d-d28f7ad2bef2",
      litellm_model_name: "hosted_vllm/zai-org/GLM-5.2-FP8",
      api_base: "http://vllm:8000/v1",
      api_provider: "hosted_vllm",
      rpm_limit: 100,
      concurrent_requests: makeTs(10, 3),
      request_rate: makeTs(10, 0.5),
      output_tokens_per_sec: makeTs(10, 50),
      latency_per_token_p50: makeTs(10, 0.005),
    },
    {
      model_id: "323f6385-5061-4ada-ba17-4fb85c770813",
      litellm_model_name: "hosted_vllm/Qwen3.6-35B-A3B-FP8",
      api_base: "http://vllm:8001/v1",
      api_provider: "hosted_vllm",
      rpm_limit: 999,
      concurrent_requests: makeTs(10, 1),
      request_rate: makeTs(10, 0.3),
      output_tokens_per_sec: makeTs(10, 30),
      latency_per_token_p50: makeTs(10, 0.003),
    },
  ],
};

const renderWithQueryClient = (ui: React.ReactElement) => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
};

describe("PerModelRealTimeView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(modelInfoCall).mockResolvedValue(mockModelInfo);
    vi.mocked(perModelMetricsCall).mockResolvedValue(mockMetricsResponse);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("renders the time window and model selectors", async () => {
    renderWithQueryClient(<PerModelRealTimeView accessToken="test-token" userID="user1" userRole="proxy_admin" />);

    await waitFor(() => {
      expect(screen.getByText("Time window:")).toBeInTheDocument();
    });
    expect(screen.getByText("Model:")).toBeInTheDocument();
  });

  it("populates the model dropdown from modelInfoCall (registered models)", async () => {
    renderWithQueryClient(<PerModelRealTimeView accessToken="test-token" userID="user1" userRole="proxy_admin" />);

    await waitFor(() => {
      expect(modelInfoCall).toHaveBeenCalledWith("test-token", "user1", "proxy_admin", 1, 100);
    });
  });

  it("calls perModelMetricsCall on mount", async () => {
    renderWithQueryClient(<PerModelRealTimeView accessToken="test-token" userID="user1" userRole="proxy_admin" />);

    await waitFor(() => {
      expect(perModelMetricsCall).toHaveBeenCalledWith("test-token", { window: "1h", model_id: undefined });
    });
  });

  it("renders deployment cards with model names from registered models when endpoint labels are empty", async () => {
    const responseWithEmptyLabels: PerModelMetricsResponse = {
      prometheus_connected: true,
      window: "1h",
      step: "30s",
      deployments: [
        {
          model_id: "a7c23c69-a80a-474a-b9d1-d0448f1eff39",
          litellm_model_name: "",
          api_base: "",
          api_provider: "",
          rpm_limit: 0,
          concurrent_requests: makeTs(10, 2),
          request_rate: makeTs(10, 0.4),
          output_tokens_per_sec: makeTs(10, 40),
          latency_per_token_p50: [],
        },
      ],
    };
    vi.mocked(perModelMetricsCall).mockResolvedValue(responseWithEmptyLabels);

    renderWithQueryClient(<PerModelRealTimeView accessToken="test-token" userID="user1" userRole="proxy_admin" />);

    await waitFor(() => {
      expect(screen.getByText("hosted_vllm/Qwen/Qwen3-Embedding-4B")).toBeInTheDocument();
    });
  });

  it("renders AreaChart elements when time-series data has multiple points", async () => {
    renderWithQueryClient(<PerModelRealTimeView accessToken="test-token" userID="user1" userRole="proxy_admin" />);

    await waitFor(() => {
      expect(screen.getByText("hosted_vllm/zai-org/GLM-5.2-FP8")).toBeInTheDocument();
    });

    const concurrentSeries = screen.getAllByTestId("series-concurrent_requests");
    expect(concurrentSeries.length).toBe(2);

    const charts = screen.getAllByTestId("tremor-area-chart");
    expect(charts.length).toBeGreaterThan(0);
  });

  it("shows no-data message when time-series has fewer than 2 points", async () => {
    const responseWithSparseData: PerModelMetricsResponse = {
      prometheus_connected: true,
      window: "1h",
      step: "30s",
      deployments: [
        {
          model_id: "e2acef83-041b-4c43-a96d-d28f7ad2bef2",
          litellm_model_name: "hosted_vllm/zai-org/GLM-5.2-FP8",
          api_base: "http://vllm:8000/v1",
          api_provider: "hosted_vllm",
          rpm_limit: 100,
          concurrent_requests: [{ timestamp: new Date().toISOString(), value: 5 }],
          request_rate: [],
          output_tokens_per_sec: [],
          latency_per_token_p50: [],
        },
      ],
    };
    vi.mocked(perModelMetricsCall).mockResolvedValue(responseWithSparseData);

    renderWithQueryClient(<PerModelRealTimeView accessToken="test-token" userID="user1" userRole="proxy_admin" />);

    await waitFor(() => {
      expect(screen.getByTestId("no-data-request_rate")).toBeInTheDocument();
    });
    expect(screen.getByTestId("no-data-output_tokens_per_sec")).toBeInTheDocument();
    expect(screen.getByTestId("no-data-latency_per_token_p50")).toBeInTheDocument();
  });

  it("shows empty state when no deployments are returned", async () => {
    vi.mocked(perModelMetricsCall).mockResolvedValue({
      prometheus_connected: true,
      window: "1h",
      step: "30s",
      deployments: [],
    });

    renderWithQueryClient(<PerModelRealTimeView accessToken="test-token" userID="user1" userRole="proxy_admin" />);

    await waitFor(() => {
      expect(screen.getByText(/no deployment metrics found/i)).toBeInTheDocument();
    });
  });

  it("shows error alert when perModelMetricsCall fails", async () => {
    vi.mocked(perModelMetricsCall).mockRejectedValue(new Error("Network error"));

    renderWithQueryClient(<PerModelRealTimeView accessToken="test-token" userID="user1" userRole="proxy_admin" />);

    await waitFor(() => {
      expect(screen.getByText(/failed to load per-model metrics/i)).toBeInTheDocument();
    });
  });
});
