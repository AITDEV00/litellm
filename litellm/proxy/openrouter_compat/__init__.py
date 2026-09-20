"""OpenRouter-compatible model discovery for LiteLLM.

Four-layer pipeline (design §2.5):
raw DTO -> DiscoveredDeploymentModel -> AggregatedModel -> OpenRouter mapper.
"""

from litellm.proxy.openrouter_compat.models_service import OpenRouterModelsService

__all__ = ["OpenRouterModelsService"]