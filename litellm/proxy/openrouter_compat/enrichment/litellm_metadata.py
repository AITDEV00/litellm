"""LiteLLM registry metadata enrichment (design §13 evidence precedence step 5).

Fills display name, huggingface id, and supported parameters from litellm's
model registry. Never overwrites smaller live deployment context with
theoretical registry limits (design §13 invariant).
"""

from __future__ import annotations

from litellm.types.utils import ModelInfo

from litellm.proxy.openrouter_compat.domain.identity import ModelIdentity
from litellm.proxy.openrouter_compat.domain.logical_model import AggregatedModel
from litellm.proxy.openrouter_compat.enrichment.registry import find_registry_model

# litellm supported_openai_params -> OpenRouter parameter names (subset we map).
# Ordering is significant: it defines the canonical order params appear in the
# OpenRouter response. Keys and values are identical (litellm already uses the
# OpenRouter name), so this is really an ordered allow-list, not an alias map.
_OPENROUTER_PARAMS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "max_tokens",
    "max_completion_tokens",
    "logprobs",
    "response_format",
    "seed",
    "stop",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "reasoning",
    "reasoning_effort",
)


class LiteLLMMetadataEnricher:
    """Fill registry-backed display/identity/parameter facts on an aggregated model."""

    def enrich(self, logical_model: AggregatedModel) -> AggregatedModel:
        registry = find_registry_model(logical_model.deployments)
        if not registry:
            return logical_model

        identity = self._enrich_identity(logical_model.identity, registry)
        return logical_model.model_copy(update={"identity": identity})

    @staticmethod
    def _enrich_identity(
        identity: ModelIdentity, registry: ModelInfo
    ) -> ModelIdentity:
        display_name = identity.display_name
        if not display_name:
            registry_name = registry.get("model_name") or identity.upstream_model_id
            if isinstance(registry_name, str):
                display_name = registry_name
        updates: dict[str, object] = {}
        if display_name:
            updates["display_name"] = display_name
        hf = identity.hugging_face_id
        if not hf:
            registry_hf = registry.get("hugging_face_id")
            if isinstance(registry_hf, str):
                updates["hugging_face_id"] = registry_hf
        return identity.model_copy(update=updates) if updates else identity

    def supported_parameters(self, logical_model: AggregatedModel) -> list[str]:
        """Resolve the OpenRouter supported_parameters list for the logical model."""
        registry = find_registry_model(logical_model.deployments)
        if not registry:
            return []
        raw = registry.get("supported_openai_params")
        if not isinstance(raw, list):
            return []
        known = {str(p) for p in raw}
        return [p for p in _OPENROUTER_PARAMS if p in known]