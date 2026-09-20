"""Logical aggregated model surfaced to the OpenRouter mapper."""

from pydantic import BaseModel, Field

from litellm.proxy.openrouter_compat.domain.architecture import ModelArchitecture
from litellm.proxy.openrouter_compat.domain.capabilities import ModelCapabilities
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.domain.identity import ModelIdentity
from litellm.proxy.openrouter_compat.domain.limits import ModelLimits


class AggregatedModel(BaseModel):
    logical_model_name: str
    deployments: list[DiscoveredDeploymentModel] = Field(default_factory=list)
    identity: ModelIdentity
    limits: ModelLimits
    architecture: ModelArchitecture
    capabilities: ModelCapabilities
    pricing: object | None = None