import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { createQueryKeys } from "../common/queryKeysFactory";
import {
  modelPerformanceCall,
  type ModelPerformanceResponse,
  type ModelPerformanceScope,
} from "@/components/networking";
import useAuthorized from "../useAuthorized";

const performanceKeys = createQueryKeys("modelPerformance");

const SHORT_WINDOWS = new Set(["5m", "15m"]);

export const useModelPerformance = (window: string = "1h", modelGroup?: string, scope: ModelPerformanceScope = {}) => {
  const { accessToken } = useAuthorized();
  const filters = {
    window,
    ...(modelGroup ? { modelGroup } : {}),
    ...scope,
  };
  return useQuery<ModelPerformanceResponse>({
    queryKey: performanceKeys.list({ filters }),
    queryFn: () => modelPerformanceCall(accessToken!, window, modelGroup, scope),
    enabled: Boolean(accessToken),
    placeholderData: keepPreviousData,
    refetchInterval: SHORT_WINDOWS.has(window) ? 30_000 : false,
  });
};
