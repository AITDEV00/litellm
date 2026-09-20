export interface ModelPerformanceTimePoint {
  timestamp: string;
  value: number | null;
}

export interface ModelPerformanceSummary {
  avg_concurrent: number;
  avg_throughput: number;
  p50_ttft: number | null;
  p95_ttft: number | null;
  total_requests: number;
  total_tokens: number;
}

export interface ModelPerformanceModel {
  model_group: string;
  time_series: {
    concurrent_requests: ModelPerformanceTimePoint[];
    throughput_tokens_per_sec: ModelPerformanceTimePoint[];
    ttft_seconds: ModelPerformanceTimePoint[];
  };
  summary: ModelPerformanceSummary;
}

export interface ModelPerformanceResponse {
  window: string;
  source: string;
  step: string;
  models: ModelPerformanceModel[];
}

export interface ModelPerformanceScope {
  teamId?: string;
  organizationId?: string;
  userId?: string;
  endUserId?: string;
  apiKey?: string;
  agentId?: string;
  /** Bucket/granularity override for the x-axis (e.g. "1 hour"). */
  step?: string;
  /** Explicit range start (ISO-8601). Overrides window-relative start. */
  startTime?: string;
  /** Explicit range end (ISO-8601). Overrides window-relative end. */
  endTime?: string;
}
