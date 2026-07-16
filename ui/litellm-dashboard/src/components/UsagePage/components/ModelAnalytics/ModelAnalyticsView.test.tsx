import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ModelAnalyticsView from "./ModelAnalyticsView";

vi.mock("../../../networking", () => ({
  modelStreamingMetricsCall: vi.fn(),
  modelLatencyMetricsCall: vi.fn(),
  modelSlowResponsesCall: vi.fn(),
  modelExceptionsCall: vi.fn(),
}));

import {
  modelExceptionsCall,
  modelLatencyMetricsCall,
  modelSlowResponsesCall,
  modelStreamingMetricsCall,
} from "../../../networking";

const renderWithQueryClient = (ui: React.ReactElement) => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
};

describe("ModelAnalyticsView", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows an info alert when no date range is provided", () => {
    renderWithQueryClient(
      <ModelAnalyticsView
        accessToken="test-token"
        modelGroups={["gpt-4", "claude-3-opus"]}
        startTime={null}
        endTime={null}
      />,
    );

    expect(screen.getByText(/select a date range to view model analytics/i)).toBeInTheDocument();
    expect(modelStreamingMetricsCall).not.toHaveBeenCalled();
    expect(modelLatencyMetricsCall).not.toHaveBeenCalled();
    expect(modelSlowResponsesCall).not.toHaveBeenCalled();
    expect(modelExceptionsCall).not.toHaveBeenCalled();
  });

  it("renders the four chart card titles and a model group selector", async () => {
    vi.mocked(modelStreamingMetricsCall).mockResolvedValue({ data: [], all_api_bases: [] });
    vi.mocked(modelLatencyMetricsCall).mockResolvedValue({ data: [], all_api_bases: [] });
    vi.mocked(modelSlowResponsesCall).mockResolvedValue([]);
    vi.mocked(modelExceptionsCall).mockResolvedValue({ data: [], exception_types: [] });

    renderWithQueryClient(
      <ModelAnalyticsView
        accessToken="test-token"
        modelGroups={["gpt-4", "claude-3-opus"]}
        startTime={new Date("2025-01-01")}
        endTime={new Date("2025-01-07")}
      />,
    );

    expect(screen.getByText("Time to First Token (seconds) over time")).toBeInTheDocument();
    expect(screen.getByText("Avg Latency per Token (seconds/token) over time")).toBeInTheDocument();
    expect(screen.getByText("Slow Responses per API Base")).toBeInTheDocument();
    expect(screen.getByText("Exceptions per API Base")).toBeInTheDocument();
    expect(screen.getByText("gpt-4")).toBeInTheDocument();
  });

  it("issues all four metric calls with the selected model group and date range", async () => {
    vi.mocked(modelStreamingMetricsCall).mockResolvedValue({ data: [], all_api_bases: [] });
    vi.mocked(modelLatencyMetricsCall).mockResolvedValue({ data: [], all_api_bases: [] });
    vi.mocked(modelSlowResponsesCall).mockResolvedValue([]);
    vi.mocked(modelExceptionsCall).mockResolvedValue({ data: [], exception_types: [] });

    renderWithQueryClient(
      <ModelAnalyticsView
        accessToken="test-token"
        modelGroups={["gpt-4"]}
        startTime={new Date("2025-01-01T00:00:00Z")}
        endTime={new Date("2025-01-07T00:00:00Z")}
      />,
    );

    await waitFor(() => {
      expect(modelStreamingMetricsCall).toHaveBeenCalledTimes(1);
      expect(modelLatencyMetricsCall).toHaveBeenCalledTimes(1);
      expect(modelSlowResponsesCall).toHaveBeenCalledTimes(1);
      expect(modelExceptionsCall).toHaveBeenCalledTimes(1);
    });

    const callArgs = vi.mocked(modelStreamingMetricsCall).mock.calls[0];
    expect(callArgs[0]).toBe("test-token");
    expect(callArgs[1].modelGroup).toBe("gpt-4");
    expect(callArgs[1].startTime).toBeInstanceOf(Date);
    expect(callArgs[1].endTime).toBeInstanceOf(Date);
  });

  it("shows a partial failure warning when one of the metric calls rejects", async () => {
    vi.mocked(modelStreamingMetricsCall).mockResolvedValue({ data: [], all_api_bases: [] });
    vi.mocked(modelLatencyMetricsCall).mockResolvedValue({ data: [], all_api_bases: [] });
    vi.mocked(modelSlowResponsesCall).mockResolvedValue([]);
    vi.mocked(modelExceptionsCall).mockRejectedValue(new Error("boom"));

    renderWithQueryClient(
      <ModelAnalyticsView
        accessToken="test-token"
        modelGroups={["gpt-4"]}
        startTime={new Date("2025-01-01")}
        endTime={new Date("2025-01-07")}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText(/failed to load some metrics/i)).toBeInTheDocument();
    });
  });

  it("renders slow responses table with computed % slow column", async () => {
    vi.mocked(modelStreamingMetricsCall).mockResolvedValue({ data: [], all_api_bases: [] });
    vi.mocked(modelLatencyMetricsCall).mockResolvedValue({ data: [], all_api_bases: [] });
    vi.mocked(modelSlowResponsesCall).mockResolvedValue([
      { api_base: "https://api.openai.com", total_count: 200, slow_count: 50 },
      { api_base: "https://api.anthropic.com", total_count: 0, slow_count: 0 },
    ]);
    vi.mocked(modelExceptionsCall).mockResolvedValue({ data: [], exception_types: [] });

    renderWithQueryClient(
      <ModelAnalyticsView
        accessToken="test-token"
        modelGroups={["gpt-4"]}
        startTime={new Date("2025-01-01")}
        endTime={new Date("2025-01-07")}
      />,
    );

    await waitFor(() => {
      expect(screen.getByText("https://api.openai.com")).toBeInTheDocument();
    });

    expect(screen.getByText("25.0%")).toBeInTheDocument();
    expect(screen.getByText("0.0%")).toBeInTheDocument();
  });

  it("renders an empty-state message when no exception data is returned", async () => {
    vi.mocked(modelStreamingMetricsCall).mockResolvedValue({ data: [], all_api_bases: [] });
    vi.mocked(modelLatencyMetricsCall).mockResolvedValue({ data: [], all_api_bases: [] });
    vi.mocked(modelSlowResponsesCall).mockResolvedValue([]);
    vi.mocked(modelExceptionsCall).mockResolvedValue({ data: [], exception_types: [] });

    renderWithQueryClient(
      <ModelAnalyticsView
        accessToken="test-token"
        modelGroups={["gpt-4"]}
        startTime={new Date("2025-01-01")}
        endTime={new Date("2025-01-07")}
      />,
    );

    await waitFor(() => {
      expect(screen.getAllByText(/no .* data/i).length).toBeGreaterThan(0);
    });
  });

  it("does not call the API when accessToken is null", () => {
    renderWithQueryClient(
      <ModelAnalyticsView
        accessToken={null}
        modelGroups={["gpt-4"]}
        startTime={new Date("2025-01-01")}
        endTime={new Date("2025-01-07")}
      />,
    );

    expect(modelStreamingMetricsCall).not.toHaveBeenCalled();
    expect(modelLatencyMetricsCall).not.toHaveBeenCalled();
    expect(modelSlowResponsesCall).not.toHaveBeenCalled();
    expect(modelExceptionsCall).not.toHaveBeenCalled();
  });
});
