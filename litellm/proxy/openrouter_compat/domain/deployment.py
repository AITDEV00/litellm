"""Canonical discovered deployment object combining all domain facts."""

from pydantic import BaseModel

from litellm.proxy.openrouter_compat.domain.architecture import ModelArchitecture
from litellm.proxy.openrouter_compat.domain.capabilities import (
    ApiCapabilities,
    ModelCapabilities,
)
from litellm.proxy.openrouter_compat.domain.identity import ModelIdentity
from litellm.proxy.openrouter_compat.domain.limits import ModelLimits
from litellm.proxy.openrouter_compat.domain.provenance import (
    ModelProvenance,
    RuntimeInfo,
)


class DiscoveredDeploymentModel(BaseModel):
    identity: ModelIdentity
    limits: ModelLimits
    architecture: ModelArchitecture
    capabilities: ModelCapabilities
    api_capabilities: ApiCapabilities
    runtime: RuntimeInfo
    provenance: ModelProvenance