import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React, { ReactNode } from "react";
import { useModelPerformance } from "./useModelPerformance";
import { modelPerformanceCall } from "@/components/UsagePage/components/ModelPerformance";
import type { ModelPerformanceScope } from "@/components/UsagePage/components/ModelPerformance";

const mockUseAuthorized = vi.fn();
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => mockUseAuthorized(),
}));

vi.mock("@/components/UsagePage/components/ModelPerformance", () => ({
  modelPerformanceCall: vi.fn(),
}));

const mockResponse = {
  models: [],
};

describe("useModelPerformance", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.clearAllMocks();
    mockUseAuthorized.mockReturnValue({ accessToken: "token-123" });
    (modelPerformanceCall as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(mockResponse);
  });

  const wrapper = ({ children }: { children: ReactNode }) =>
    React.createElement(QueryClientProvider, { client: queryClient }, children);

  it("forwards the entity scope to the API call", async () => {
    const scope: ModelPerformanceScope = { teamId: "team-abc", userId: "user-xyz" };
    renderHook(() => useModelPerformance({ window: "1h", scope }), { wrapper });

    await waitFor(() => {
      expect(modelPerformanceCall).toHaveBeenCalledWith("token-123", "1h", undefined, scope);
    });
  });

  it("uses an empty scope by default", async () => {
    renderHook(() => useModelPerformance({ window: "24h" }), { wrapper });

    await waitFor(() => {
      expect(modelPerformanceCall).toHaveBeenCalledWith("token-123", "24h", undefined, {});
    });
  });

  it.each([
    { live: true, window: "24h", expected: 10_000 },
    { live: false, window: "5m", expected: 30_000 },
    { live: false, window: "24h", expected: false },
  ])("refreshes live=$live window=$window at $expected ms", async ({ live, window, expected }) => {
    const { result } = renderHook(() => useModelPerformance({ window, live }), { wrapper });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    await waitFor(() => {
      const query = queryClient.getQueryCache().findAll().at(0);
      expect(query?.options.refetchInterval).toBe(expected);
    });
  });
});
