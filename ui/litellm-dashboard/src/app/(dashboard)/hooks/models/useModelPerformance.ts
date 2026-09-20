import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { createQueryKeys } from "../common/queryKeysFactory";
import {
  modelPerformanceCall,
  type ModelPerformanceResponse,
  type ModelPerformanceScope,
} from "@/components/UsagePage/components/ModelPerformance";
import useAuthorized from "../useAuthorized";

const performanceKeys = createQueryKeys("modelPerformance");

const SHORT_WINDOWS = new Set(["5m", "15m"]);

export const useModelPerformance = (
  window: string = "1h",
  modelGroup?: string,
  scope: ModelPerformanceScope = {},
  step?: string,
  live: boolean = false,
) => {
  const { accessToken } = useAuthorized();
  const effectiveScope: ModelPerformanceScope = step == null || step === "" ? scope : { ...scope, step };
  const filters = {
    window,
    ...(modelGroup ? { modelGroup } : {}),
    ...effectiveScope,
  };
  return useQuery<ModelPerformanceResponse>({
    queryKey: performanceKeys.list({ filters }),
    queryFn: () => modelPerformanceCall(accessToken!, window, modelGroup, effectiveScope),
    enabled: Boolean(accessToken),
    placeholderData: keepPreviousData,
    // Live mode ticks continuously; short windows without live mode refresh
    // every 30s as before.
    refetchInterval: live ? 10_000 : SHORT_WINDOWS.has(window) ? 30_000 : false,
  });
};
