import { apiClient } from "../../../networking";
import type { ModelPerformanceResponse, ModelPerformanceScope } from "./types";

export const modelPerformanceCall = async (
  accessToken: string,
  window: string = "1h",
  modelGroup?: string,
  scope: ModelPerformanceScope = {},
): Promise<ModelPerformanceResponse> => {
  try {
    return await apiClient.get(`/model/performance`, {
      accessToken,
      query: {
        window,
        ...(scope.step ? { step: scope.step } : {}),
        ...(modelGroup ? { model_group: modelGroup } : {}),
        ...(scope.teamId ? { team_id: scope.teamId } : {}),
        ...(scope.organizationId ? { organization_id: scope.organizationId } : {}),
        ...(scope.userId ? { user_id: scope.userId } : {}),
        ...(scope.endUserId ? { end_user_id: scope.endUserId } : {}),
        ...(scope.apiKey ? { api_key: scope.apiKey } : {}),
        ...(scope.agentId ? { agent_id: scope.agentId } : {}),
        ...(scope.startTime ? { start_time: scope.startTime } : {}),
        ...(scope.endTime ? { end_time: scope.endTime } : {}),
      },
    });
  } catch (error) {
    console.error("Failed to fetch model performance:", error);
    throw error;
  }
};
