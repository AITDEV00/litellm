import { describe, it, expect } from "vitest";

/**
 * Drop-detection regression tests for the OICM-custom Model Performance feature.
 *
 * The Model Performance UI + API are OICM-custom and historically had their
 * types + API call grafted inline into the upstream `networking.tsx` file. An
 * upstream merge could drop that graft silently. The types + call now live in
 * this co-located slice; `networking.tsx` only re-exports them. These tests pin
 * the wiring so a dropped graft fails loudly instead of disappearing.
 */

describe("ModelPerformance slice exports", () => {
  it("exposes the API fetch function from the slice package", async () => {
    const slice = await import("@/components/UsagePage/components/ModelPerformance");
    expect(typeof slice.modelPerformanceCall).toBe("function");
  });

  it("still re-exports the slice from networking.tsx for backward compat", async () => {
    const networking = await import("@/components/networking");
    expect(typeof networking.modelPerformanceCall).toBe("function");
  });
});
