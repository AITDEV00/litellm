import { describe, expect, it } from "vitest";

/**
 * Guards the fork's UI slices against being silently dropped by an upstream
 * merge, the way the v1.102.0 and v1.108.5 merges dropped the Model Performance
 * mounts without failing a test. Each case names one wiring point that a merge
 * can delete on its own while the slice code it feeds still exists.
 *
 * Keep this list in sync with
 * ../../oicm-litellm-layer/docs/techniques/upstream_merge_technique.md
 */
const REQUIRED_MODULES: readonly { module: string; exports: readonly string[] }[] = [
  {
    module: "@/components/UsagePage/components/ModelPerformance",
    exports: ["modelPerformanceCall"],
  },
  {
    module: "@/components/shared/charts",
    exports: ["LineChart"],
  },
  {
    module: "@/components/networking",
    exports: ["modelPerformanceCall"],
  },
];

describe("fork slice wiring", () => {
  it.each(REQUIRED_MODULES)("$module still exports its public surface", async ({ module, exports }) => {
    const loaded: Record<string, unknown> = await import(/* @vite-ignore */ module);
    for (const name of exports) {
      expect(loaded[name], `${module} is missing the ${name} export`).toBeDefined();
    }
  });
});
