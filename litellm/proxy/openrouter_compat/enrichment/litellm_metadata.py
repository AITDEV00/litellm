"""LiteLLM registry metadata enrichment (design §13 evidence precedence step 5).

Fills display name, huggingface id, and supported parameters from litellm's
model registry. Never overwrites smaller live deployment context with
theoretical registry limits (design §13 invariant).
"""

from __future__ import annotations

import litellm
from litellm.types.utils import ModelInfo

from litellm.proxy.openrouter_compat.domain.identity import ModelIdentity
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.domain.logical_model import AggregatedModel

# litellm supported_openai_params -> OpenRouter parameter names (subset we map).
_OPENROUTER_PARAM_ALIASES: dict[str, str] = {
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
    "max_tokens": "max_tokens",
    "max_completion_tokens": "max_completion_tokens",
    "logprobs": "logprobs",
    "response_format": "response_format",
    "seed": "seed",
    "stop": "stop",
    "tools": "tools",
    "tool_choice": "tool_choice",
    "parallel_tool_calls": "parallel_tool_calls",
    "reasoning": "reasoning",
    "reasoning_effort": "reasoning_effort",
}


class LiteLLMMetadataEnricher:
    """Fill registry-backed display/identity/parameter facts on an aggregated model."""

    def enrich(self, logical_model: AggregatedModel) -> AggregatedModel:
        registry = self._registry_for(logical_model.deployments)
        if not registry:
            return logical_model

        identity = self._enrich_identity(logical_model.identity, registry)
        return logical_model.model_copy(update={"identity": identity})

    @staticmethod
    def _registry_for(
        deployments: list[DiscoveredDeploymentModel],
    ) -> ModelInfo | None:
        for deployment in deployments:
            model_name = deployment.identity.upstream_model_id
            if not model_name:
                continue
            try:
                return litellm.get_model_info(model_name)
            except Exception:
                continue
        return None

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
        registry = self._registry_for(logical_model.deployments)
        if not registry:
            return []
        raw = registry.get("supported_openai_params")
        if not isinstance(raw, list):
            return []
        known = {_OPENROUTER_PARAM_ALIASES.get(str(p), str(p)) for p in raw}
        ordered = [
            p for p in _OPENROUTER_PARAM_ALIASES.values() if p in known
        ]
        return ordered