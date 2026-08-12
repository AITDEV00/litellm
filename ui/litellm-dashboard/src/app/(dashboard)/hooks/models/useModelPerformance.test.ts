import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React, { ReactNode } from "react";
import { useModelPerformance } from "./useModelPerformance";
import { modelPerformanceCall } from "@/components/networking";
import type { ModelPerformanceScope } from "@/components/networking";

const mockUseAuthorized = vi.fn();
vi.mock("@/app/(dashboard)/hooks/useAuthorized", () => ({
  default: () => mockUseAuthorized(),
}));

vi.mock("@/components/networking", () => ({
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
    renderHook(() => useModelPerformance("1h", undefined, scope), { wrapper });

    await waitFor(() => {
      expect(modelPerformanceCall).toHaveBeenCalledWith("token-123", "1h", undefined, scope);
    });
  });

  it("uses an empty scope by default", async () => {
    renderHook(() => useModelPerformance("24h"), { wrapper });

    await waitFor(() => {
      expect(modelPerformanceCall).toHaveBeenCalledWith("token-123", "24h", undefined, {});
    });
  });
});
