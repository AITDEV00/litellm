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

const SUSTAINED_REFRESH_MS = 30_000;
const LIVE_REFRESH_MS = 10_000;

/**
 * Live mode ticks continuously; short Prometheus-backed windows refresh every
 * 30s and every other window stays static until the user acts.
 */
const refreshInterval = (live: boolean, window: string): number | false => {
  if (live) return LIVE_REFRESH_MS;
  if (SHORT_WINDOWS.has(window)) return SUSTAINED_REFRESH_MS;
  return false;
};

export type ModelPerformanceQueryOptions = {
  window?: string;
  modelGroup?: string;
  scope?: ModelPerformanceScope;
  step?: string;
  live?: boolean;
};

export const useModelPerformance = ({
  window = "1h",
  modelGroup,
  scope = {},
  step,
  live = false,
}: ModelPerformanceQueryOptions = {}) => {
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
    refetchInterval: refreshInterval(live, window),
  });
};
