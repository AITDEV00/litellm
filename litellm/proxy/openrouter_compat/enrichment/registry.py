"""Shared litellm model-registry lookup for enrichment layers."""

from __future__ import annotations

import litellm
from litellm.types.utils import ModelInfo

from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel


def find_registry_model(
    deployments: list[DiscoveredDeploymentModel],
) -> ModelInfo | None:
    """Return the first litellm registry entry matching an upstream model id."""
    for deployment in deployments:
        model_name = deployment.identity.upstream_model_id
        if not model_name:
            continue
        try:
            return litellm.get_model_info(model_name)
        except Exception:
            continue
    return None