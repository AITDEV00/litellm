import { describe, it, expect, vi, beforeEach } from "vitest";
import { modelPerformanceCall } from "./api";
import type { ModelPerformanceResponse } from "./types";

/**
 * Contract tests for the OICM-custom Model Performance API client.
 *
 * The backend contract is documented in
 * oicm-litellm-layer/docs/performance/ and pinned by the gateway smoke suite
 * (tests/smoke/test_contract.py::TestModelPerformance). These tests pin the
 * client side: the query params it builds and the response shape it returns.
 * An upstream merge that changes the param names or drops a scope field fails
 * here instead of silently breaking the dashboard.
 */

const { apiGetMock } = vi.hoisted(() => ({ apiGetMock: vi.fn() }));

vi.mock("../../../networking", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../networking")>();
  return { ...actual, apiClient: { ...actual.apiClient, get: apiGetMock } };
});

const RESPONSE: ModelPerformanceResponse = {
  window: "1h",
  source: "prometheus",
  step: "30s",
  models: [],
};

beforeEach(() => {
  apiGetMock.mockClear();
});

describe("modelPerformanceCall", () => {
  it("builds the documented query params", async () => {
    apiGetMock.mockResolvedValue(RESPONSE);
    await modelPerformanceCall("tok", "24h", "Qwen/GLM", {
      step: "1 hour",
      teamId: "team-1",
      organizationId: "org-1",
      userId: "user-1",
      endUserId: "end-1",
      apiKey: "key-1",
      agentId: "agent-1",
      startTime: "2026-11-01",
      endTime: "2026-11-02",
    });

    expect(apiGetMock).toHaveBeenCalledOnce();
    const [path, opts] = apiGetMock.mock.calls[0];
    expect(path).toBe("/model/performance");
    expect(opts.accessToken).toBe("tok");
    expect(opts.query).toEqual({
      window: "24h",
      step: "1 hour",
      model_group: "Qwen/GLM",
      team_id: "team-1",
      organization_id: "org-1",
      user_id: "user-1",
      end_user_id: "end-1",
      api_key: "key-1",
      agent_id: "agent-1",
      start_time: "2026-11-01",
      end_time: "2026-11-02",
    });
  });

  it("omits unset scope fields rather than sending empty values", async () => {
    apiGetMock.mockResolvedValue(RESPONSE);
    await modelPerformanceCall("tok");

    const [, opts] = apiGetMock.mock.calls[0];
    // Defaults: window only, no scope keys at all.
    expect(opts.query).toEqual({ window: "1h" });
    expect(opts.query).not.toHaveProperty("model_group");
    expect(opts.query).not.toHaveProperty("team_id");
  });

  it("returns the documented response shape", async () => {
    apiGetMock.mockResolvedValue(RESPONSE);
    const result = await modelPerformanceCall("tok");

    expect(result.window).toBe("1h");
    expect(result.source).toBe("prometheus");
    expect(result.step).toBe("30s");
    expect(Array.isArray(result.models)).toBe(true);
  });

  it("rethrows backend errors so the caller surfaces them", async () => {
    apiGetMock.mockRejectedValue(new Error("500: boom"));
    await expect(modelPerformanceCall("tok")).rejects.toThrow("500: boom");
  });
});
