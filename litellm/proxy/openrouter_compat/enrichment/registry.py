"""Shared litellm model-registry lookup for enrichment layers."""

from __future__ import annotations

from typing import Final

import litellm
from pydantic import TypeAdapter
from litellm.types.utils import ModelInfo

from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel

_STR_LIST_ADAPTER: Final[TypeAdapter[list[str]]] = TypeAdapter(list[str])


def _deployment_provider(deployment: DiscoveredDeploymentModel) -> str | None:
    raw = deployment.model_info.get("custom_llm_provider")
    return raw if isinstance(raw, str) and raw else None


def find_registry_model(
    deployments: list[DiscoveredDeploymentModel],
) -> ModelInfo | None:
    """Return the first litellm registry entry matching an upstream model id.

    Falls back to the provider config's supported params when the model id is
    not in the cost map, so provider-backed deployments (e.g. hosted-vLLM
    serving an unmapped Qwen id) still advertise real capabilities instead
    of an empty list."""
    for deployment in deployments:
        model_name = deployment.identity.upstream_model_id
        if not model_name:
            continue
        provider = _deployment_provider(deployment)
        try:
            return litellm.get_model_info(model_name, custom_llm_provider=provider)
        except Exception:  # noqa: BLE001, S110  # unmapped ids fall through to the provider-config lookup below
            pass
        if provider is not None:
            try:
                raw_params = litellm.get_supported_openai_params(  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]  # core util is untyped; validated below
                    model=model_name, custom_llm_provider=provider
                )
            except Exception:  # noqa: BLE001, S112  # unknown providers stay honestly empty
                continue
            params = _STR_LIST_ADAPTER.validate_python(raw_params) if raw_params is not None else None
            if params:
                return ModelInfo(
                    key=model_name,
                    max_tokens=None,
                    max_input_tokens=None,
                    max_output_tokens=None,
                    input_cost_per_token=0,
                    output_cost_per_token=0,
                    litellm_provider=provider,
                    mode="chat",
                    supported_openai_params=params,
                )
    return None