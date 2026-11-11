"""Tests for the OpenRouter mapper (design §22).

Regression focus: canonical slugting, modality defaults, pricing formatting,
and the OpenRouter `Model` serialization contract (no sentinel leaks, no null
fields for unset optionals).
"""

from __future__ import annotations

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
    ModelProvenance,
    RuntimeInfo,
)
from litellm.proxy.openrouter_compat.enrichment.litellm_metadata import (
    LiteLLMMetadataEnricher,
)
from litellm.proxy.openrouter_compat.enrichment.pricing import Pricing, PricingResolver
from litellm.proxy.openrouter_compat.mapping.openrouter import OpenRouterModelMapper
from litellm.proxy.openrouter_compat.openrouter_schema.base import UNSET_SENTINEL
from litellm.proxy.openrouter_compat.openrouter_schema.models import Model


def _aggregated(
    *,
    logical: str = "gpt-x",
    context: int = 131072,
    max_completion: int | None = 4096,
    display_name: str | None = "GPT-X",
    hugging_face_id: str | None = "org/gpt-x",
    created: int | None = 1710000000,
) -> AggregatedModel:
    deployment = DiscoveredDeploymentModel(
        identity=ModelIdentity(
            logical_model_name=logical,
            upstream_model_id="openai/gpt-x",
            display_name=display_name,
            hugging_face_id=hugging_face_id,
            created=created,
        ),
        limits=ModelLimits(
            context_length=context,
            max_input_tokens=8192,
            max_completion_tokens=max_completion,
        ),
        architecture=ModelArchitecture(model_type="gpt"),
        capabilities=ModelCapabilities(
            input_modalities={"text"},
            output_modalities={"text"},
            tool_calling=True,
        ),
        api_capabilities=ApiCapabilities(chat_completions=True),
        runtime=RuntimeInfo(kind="openai-compatible", deployment_id="dep-1"),
        provenance=ModelProvenance(),
    )
    return AggregatedModel(
        logical_model_name=logical,
        deployments=[deployment],
        identity=deployment.identity,
        limits=deployment.limits,
        architecture=deployment.architecture,
        capabilities=deployment.capabilities,
    )


class _FakePricingResolver(PricingResolver):
    def resolve(self, logical_model):
        return Pricing(prompt="0.30", completion="1.20")


def test_canonical_slug_bare_namespace():
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    model = mapper.map_model(_aggregated(logical="gpt-x"))
    assert model.canonical_slug == "litellm/gpt-x"
    assert model.links.details == "http://proxy:4000/api/v1/models/litellm/gpt-x/endpoints"


def test_canonical_slug_keeps_author_qualified():
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    model = mapper.map_model(_aggregated(logical="anthropic/claude-3"))
    assert model.canonical_slug == "anthropic/claude-3"


def test_mapper_uses_resolved_pricing():
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000", pricing_resolver=_FakePricingResolver())
    model = mapper.map_model(_aggregated())
    assert model.pricing.prompt == "0.30"
    assert model.pricing.completion == "1.20"


def test_mapper_defaults_pricing_to_zero_when_unknown():
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    model = mapper.map_model(_aggregated())
    assert model.pricing.prompt == "0"
    assert model.pricing.completion == "0"


def test_mapper_maps_known_text_modalities():
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    model = mapper.map_model(_aggregated())
    assert [str(m) for m in model.architecture.input_modalities] == ["text"]
    assert model.architecture.modality == "text"


def test_mapper_unknown_modalities_are_empty_not_text():
    # No runtime/registry evidence -> honest unknown: empty arrays, None modality.
    model = _aggregated()
    no_modalities = model.model_copy(
        update={
            "capabilities": model.capabilities.model_copy(update={"input_modalities": None, "output_modalities": None})
        }
    )
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    mapped = mapper.map_model(no_modalities)
    assert mapped.architecture.input_modalities == []
    assert mapped.architecture.output_modalities == []
    assert mapped.architecture.modality is None


def test_mapper_no_unset_sentinel_leak_in_json():
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    model = mapper.map_model(_aggregated())
    raw = model.model_dump_json()
    assert UNSET_SENTINEL not in raw


def test_mapper_serialization_roundtrip_matches_openrouter_shape():
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    model = mapper.map_model(_aggregated())
    data = model.model_dump()
    assert set(data) == {
        "architecture",
        "canonical_slug",
        "context_length",
        "created",
        "default_parameters",
        *({"endpoints"} if "endpoints" in Model.model_fields else set()),
        "id",
        "links",
        "name",
        "per_request_limits",
        "pricing",
        "supported_parameters",
        "supported_voices",
        "top_provider",
        "hugging_face_id",
    }
    assert data["id"] == "gpt-x"
    assert data["name"] == "GPT-X"
    assert data["context_length"] == 131072
    assert data["per_request_limits"] == {
        "prompt_tokens": 8192.0,
        "completion_tokens": 4096.0,
    }
    assert data["top_provider"]["is_moderated"] is False
    assert data["architecture"]["input_modalities"] == ["text"]


def test_mapper_declares_only_discovered_inference_paths():
    model = _aggregated()
    discovered = model.deployments[0].model_copy(
        update={
            "api_capabilities": model.deployments[0].api_capabilities.model_copy(
                update={"chat_completions": True, "transcription": True, "routes": {"/v1/systemone"}}
            )
        }
    )
    mapped = OpenRouterModelMapper(details_base_url="http://proxy:4000").map_model(
        model.model_copy(update={"deployments": [discovered]})
    )

    if "endpoints" in type(mapped).model_fields:
        assert mapped.endpoints == ["/v1/audio/transcriptions", "/v1/chat/completions", "/v1/systemone"]


def test_supported_parameters_placeholder_covers_full_openrouter_set(monkeypatch):
    # Regression: the ordered allow-list must hold a placeholder for every
    # capability/parameter OpenRouter can advertise, so extending the registry
    # with a new supported param never silently drops it from the public
    # response. top_a / top_logprobs / structured_outputs etc. are part of the
    # OpenRouter Parameter union and must be map-pable.
    import litellm.proxy.openrouter_compat.enrichment.litellm_metadata as meta_mod
    from litellm.proxy.openrouter_compat.enrichment.litellm_metadata import (
        LiteLLMMetadataEnricher,
    )

    registry = {
        "model_name": "gpt-x",
        "supported_openai_params": [
            "temperature",
            "top_a",
            "top_logprobs",
            "structured_outputs",
            "frequency_penalty",
            "web_search_options",
            "verbosity",
        ],
    }

    monkeypatch.setattr(meta_mod, "find_registry_model", lambda deployments: registry)

    params = LiteLLMMetadataEnricher().supported_parameters(_aggregated())
    assert params == [
        "temperature",
        "top_a",
        "frequency_penalty",
        "top_logprobs",
        "structured_outputs",
        "web_search_options",
        "verbosity",
    ]


def test_supported_parameters_resolve_via_provider_hint() -> None:
    """Regression: a provider-backed deployment whose upstream model id is not
    in the litellm cost map must still advertise real capabilities.

    Live case (dev): Qwen/Qwen3-Next-80B-A3B-Instruct served by hosted-vLLM
    listed valid /api/v1/models endpoints but supported_parameters=[] because
    the registry lookup never passed the deployment's custom_llm_provider, and
    the unmapped id missed. The provider-aware fallback must fill the params
    the provider config actually supports."""
    model = _aggregated(logical="qwen-hosted")
    deployment = model.deployments[0].model_copy(
        update={
            "model_info": {"custom_llm_provider": "hosted_vllm"},
            "identity": model.deployments[0].identity.model_copy(
                update={"upstream_model_id": "Qwen/Qwen3-Next-80B-A3B-Instruct"}
            ),
        }
    )
    aggregated = model.model_copy(update={"deployments": [deployment]})

    params = LiteLLMMetadataEnricher().supported_parameters(aggregated)
    assert params, "provider-backed deployment must not advertise zero supported parameters"
    assert "temperature" in params
    assert "tools" in params
    assert "reasoning_effort" in params


def test_supported_parameters_stay_empty_for_unknown_provider() -> None:
    """No provider hint and no cost-map match must remain honestly empty
    rather than fabricating a capability list."""
    model = _aggregated()
    deployment = model.deployments[0].model_copy(
        update={
            "model_info": {"custom_llm_provider": "not_a_real_provider"},
            "identity": model.deployments[0].identity.model_copy(
                update={"upstream_model_id": "Qwen/Qwen3-Next-80B-A3B-Instruct"}
            ),
        }
    )
    aggregated = model.model_copy(update={"deployments": [deployment]})

    params = LiteLLMMetadataEnricher().supported_parameters(aggregated)
    assert params == []


def test_mapper_emits_provider_supported_parameters() -> None:
    """End-to-end through the mapper: the public Model response must carry
    the provider-resolved supported_parameters list."""
    model = _aggregated(logical="qwen-hosted")
    deployment = model.deployments[0].model_copy(
        update={
            "model_info": {"custom_llm_provider": "hosted_vllm"},
            "identity": model.deployments[0].identity.model_copy(
                update={"upstream_model_id": "Qwen/Qwen3-Next-80B-A3B-Instruct"}
            ),
        }
    )
    mapped = OpenRouterModelMapper(details_base_url="http://proxy:4000").map_model(
        model.model_copy(update={"deployments": [deployment]})
    )
    assert [str(p) for p in mapped.supported_parameters] != []
    assert "reasoning_effort" in [str(p) for p in mapped.supported_parameters]


def test_supported_parameters_use_registered_deployment_params() -> None:
    """Regression (live on dev): OICM-registered deployments carry their own
    supported_openai_params in model_info, but the registry lookup only
    consulted the litellm cost map, so unmapped ids (Qwen/Qwen3-Next-80B via
    hosted-vLLM, custom_llm_provider unset) advertised supported_parameters=[]
    despite valid endpoints. The registered params must be used when the cost
    map misses."""
    registry_params: list[str] = [
        "temperature",
        "top_p",
        "tools",
        "tool_choice",
        "reasoning_effort",
        "store",
    ]
    model = _aggregated(logical="qwen-registered")
    deployment = model.deployments[0].model_copy(
        update={
            "model_info": {"supported_openai_params": registry_params, "litellm_provider": "hosted_vllm"},
            "identity": model.deployments[0].identity.model_copy(
                update={"upstream_model_id": "Qwen/Qwen3-Next-80B-A3B-Instruct"}
            ),
        }
    )
    aggregated = model.model_copy(update={"deployments": [deployment]})

    params = LiteLLMMetadataEnricher().supported_parameters(aggregated)
    # store is not in the OpenRouter parameter union, so it is filtered out.
    assert params == ["temperature", "top_p", "tools", "tool_choice", "reasoning_effort"]


def test_resolver_derives_provider_from_model_string_prefix() -> None:
    """Regression (live on dev): OICM-registered deployments carry the provider
    in the litellm_params.model prefix (hosted_vllm/Qwen/...) with the
    custom_llm_provider field unset, so provider-aware capability lookup never
    fired and /api/v1/models advertised supported_parameters=[] for models
    with valid endpoints. The descriptor must derive the provider from the
    prefix."""
    from litellm.types.router import Deployment

    from litellm.proxy.openrouter_compat.discovery.resolver import DeploymentResolver

    deployment = Deployment(
        model_name="Qwen/Qwen3-Next-80B-A3B-Instruct",
        litellm_params={"model": "hosted_vllm/Qwen/Qwen3-Next-80B-A3B-Instruct", "api_base": "http://vllm:8000"},
        model_info={"id": "dep-1", "db_model": True},
    )
    descriptor = DeploymentResolver(None)._build_descriptor(deployment, "Qwen/Qwen3-Next-80B-A3B-Instruct")
    assert descriptor is not None
    assert descriptor.provider == "hosted_vllm"
    assert descriptor.model_info["custom_llm_provider"] == "hosted_vllm"


def test_resolver_keeps_explicit_provider_over_prefix() -> None:
    """An explicit custom_llm_provider must win over the prefix-derived one."""
    from litellm.types.router import Deployment

    from litellm.proxy.openrouter_compat.discovery.resolver import DeploymentResolver

    deployment = Deployment(
        model_name="custom-model",
        litellm_params={
            "model": "hosted_vllm/custom-model",
            "api_base": "http://vllm:8000",
            "custom_llm_provider": "vllm",
        },
        model_info={"id": "dep-2", "db_model": True},
    )
    descriptor = DeploymentResolver(None)._build_descriptor(deployment, "custom-model")
    assert descriptor is not None
    assert descriptor.provider == "vllm"


def test_mapper_omits_nullable_optionals_when_unset():
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    model = mapper.map_model(_aggregated(max_completion=None))
    data = model.model_dump()
    # max_input_tokens is still 8192 -> per_request_limits present.
    assert data["per_request_limits"]["completion_tokens"] == 0.0
    assert data["per_request_limits"]["prompt_tokens"] == 8192.0
    # hugging_face_id provided -> present
    assert data["hugging_face_id"] == "org/gpt-x"


def test_mapper_placeholder_is_conformant_and_informative():
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    placeholder = mapper.map_placeholder("broken-model")
    data = placeholder.model_dump()
    assert data["id"] == "broken-model"
    assert data["canonical_slug"] == "litellm/broken-model"
    assert data["context_length"] == 0
    assert data["architecture"]["input_modalities"] == []
    assert data["architecture"]["output_modalities"] == []
    assert data["architecture"]["modality"] is None
    assert data["per_request_limits"] is None
    assert placeholder.description is not None
    assert "not properly configured or deployed" in placeholder.description
    # Placeholder still round-trips as a plain dict without sentinels.
    serialized = placeholder.model_dump_json()
    assert UNSET_SENTINEL not in serialized
    assert "broken-model" in serialized
