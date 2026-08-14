"""Capability enrichment. Intersect runtime evidence with litellm registry (design §23)."""

from __future__ import annotations

from litellm.proxy.openrouter_compat.domain.logical_model import AggregatedModel
from litellm.proxy.openrouter_compat.enrichment.registry import find_registry_model

# litellm registry capability fields we reuse to fill unknown (None) facts.
_LITELLM_FIELD_MAP: dict[str, str] = {
    "tool_calling": "supports_function_calling",
    "parallel_tool_calling": "supports_tool_choice",
    "reasoning": "supports_reasoning",
    "structured_outputs": "supports_response_schema",
}


class CapabilityEnricher:
    """Fill unknown model capabilities from litellm's registry when known."""

    def enrich(
        self, logical_model: AggregatedModel
    ) -> AggregatedModel:
        if not logical_model.deployments:
            return logical_model
        registry = find_registry_model(logical_model.deployments)
        if not registry:
            return logical_model
        caps = logical_model.capabilities
        updates: dict[str, object] = {}
        for field, registry_key in _LITELLM_FIELD_MAP.items():
            if getattr(caps, field) is None and registry.get(registry_key) is True:
                updates[field] = True
        if not updates:
            return logical_model
        new_caps = caps.model_copy(update=updates)
        return logical_model.model_copy(update={"capabilities": new_caps})