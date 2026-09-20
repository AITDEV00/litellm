"""Canonical domain model for OpenRouter-compatible model discovery.

No runtime or OpenRouter imports here. Raw runtime DTOs normalize into these
types before aggregation and OpenRouter mapping.
"""

from litellm.proxy.openrouter_compat.domain.architecture import ModelArchitecture
from litellm.proxy.openrouter_compat.domain.capabilities import (
    ApiCapabilities,
    ModelCapabilities,
)
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.domain.identity import ModelIdentity
from litellm.proxy.openrouter_compat.domain.limits import ModelLimits
from litellm.proxy.openrouter_compat.domain.logical_model import AggregatedModel
from litellm.proxy.openrouter_compat.domain.provenance import (
    FactSource,
    ModelProvenance,
    RuntimeInfo,
)

__all__ = [
    "AggregatedModel",
    "ApiCapabilities",
    "DiscoveredDeploymentModel",
    "FactSource",
    "ModelArchitecture",
    "ModelCapabilities",
    "ModelIdentity",
    "ModelLimits",
    "ModelProvenance",
    "RuntimeInfo",
]