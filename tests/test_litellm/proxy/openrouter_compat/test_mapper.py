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
from litellm.proxy.openrouter_compat.enrichment.pricing import Pricing, PricingResolver
from litellm.proxy.openrouter_compat.mapping.openrouter import OpenRouterModelMapper
from litellm.proxy.openrouter_compat.openrouter_schema.base import UNSET_SENTINEL


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
