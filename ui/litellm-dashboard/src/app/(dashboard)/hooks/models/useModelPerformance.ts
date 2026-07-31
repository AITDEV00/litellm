import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { createQueryKeys } from "../common/queryKeysFactory";
import { modelPerformanceCall, type ModelPerformanceResponse } from "@/components/networking";
import useAuthorized from "../useAuthorized";

const performanceKeys = createQueryKeys("modelPerformance");

const SHORT_WINDOWS = new Set(["5m", "15m"]);

export const useModelPerformance = (window: string = "1h", modelGroup?: string) => {
  const { accessToken } = useAuthorized();
  return useQuery<ModelPerformanceResponse>({
    queryKey: performanceKeys.list({
      filters: { window, ...(modelGroup ? { modelGroup } : {}) },
    }),
    queryFn: async () => await modelPerformanceCall(accessToken!, window, modelGroup),
    enabled: Boolean(accessToken),
    placeholderData: keepPreviousData,
    refetchInterval: SHORT_WINDOWS.has(window) ? 30_000 : false,
  });
};
