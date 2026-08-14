"""End-to-end integration tests for the discovery pipeline (design §37.8).

Drive registry -> DiscoveryService -> adapters -> aggregation -> mapper with a
fake HTTP client, then validate the result serializes through the real
OpenRouter SDK into the expected public shape.
"""

from __future__ import annotations

from litellm.proxy.openrouter_compat.aggregation.aggregator import ModelAggregator
from litellm.proxy.openrouter_compat.discovery.registry import DiscoveryAdapterRegistry
from litellm.proxy.openrouter_compat.discovery.resolver import DeploymentDescriptor
from litellm.proxy.openrouter_compat.mapping.openrouter import OpenRouterModelMapper
from litellm.proxy.openrouter_compat.service import DiscoveryService
from litellm.proxy.openrouter_compat.transport.client import DiscoveryTarget
from litellm.proxy.openrouter_compat.transport.errors import DiscoveryHTTPError


class FakeClient:
    """Duck-typed discovery HTTP client returning per-route fixtures."""

    def __init__(self, routes: dict[str, dict[str, object]]) -> None:
        self._routes = {
            (api_base, path): payload
            for api_base, path_payload in routes.items()
            for path, payload in path_payload.items()
        }

    async def get_json(self, target: DiscoveryTarget, path: str) -> dict[str, object]:
        value = self._routes.get((target.api_base, path))
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise DiscoveryHTTPError(404, f"no route {path}")
        return dict(value)  # type: ignore[arg-type]

    async def aclose(self) -> None:
        return None


def _sglang_descriptor() -> DeploymentDescriptor:
    return DeploymentDescriptor(
        deployment_id="sglang-dep",
        logical_model_name="deepseek-v4",
        provider="sglang",
        model="deepseek-v4",
        api_base="http://sglang:8000",
        model_info={"discovery_runtime": "sglang"},
    )


def _vllm_descriptor() -> DeploymentDescriptor:
    return DeploymentDescriptor(
        deployment_id="vllm-dep",
        logical_model_name="qwen3.5-122b",
        provider="vllm",
        model="qwen3.5-122b",
        api_base="http://vllm:8000",
        model_info={"discovery_runtime": "vllm"},
    )


def _sglang_routes() -> dict[str, dict[str, object]]:
    return {
        "http://sglang:8000": {
            "/v1/models": {"data": [{"id": "deepseek-v4", "max_model_len": 262144}]},
            "/model_info": {
                "is_generation": True,
                "has_image_understanding": True,
                "has_audio_understanding": False,
                "model_type": "deepseek_v4",
                "architectures": ["DeepseekV4ForCausalLM"],
            },
        }
    }


def _vllm_routes() -> dict[str, dict[str, object]]:
    return {
        "http://vllm:8000": {
            "/v1/models": {"data": [{"id": "qwen3.5-122b", "max_model_len": 131072}]},
            "/openapi.json": {
                "paths": {
                    "/v1/chat/completions": {"post": {}},
                    "/v1/embeddings": {"post": {}},
                },
                "components": {"schemas": {}},
            },
        }
    }


def _merge(*route_maps: dict[str, dict[str, object]]) -> dict[str, dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for route_map in route_maps:
        for api_base, payloads in route_map.items():
            merged.setdefault(api_base, {}).update(payloads)
    return merged


async def test_full_pipeline_maps_both_runtimes():
    client = FakeClient(_merge(_sglang_routes(), _vllm_routes()))
    registry = DiscoveryAdapterRegistry(client)
    service = DiscoveryService(registry)
    result = await service.discover_many([_sglang_descriptor(), _vllm_descriptor()])
    assert len(result.discoveries) == 2
    assert result.failed_logical_models == set()
    discoveries = result.discoveries

    aggregated = ModelAggregator().aggregate_all(discoveries)
    assert {m.logical_model_name for m in aggregated} == {
        "deepseek-v4",
        "qwen3.5-122b",
    }

    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    sglang = next(m for m in aggregated if m.logical_model_name == "deepseek-v4")
    vllm = next(m for m in aggregated if m.logical_model_name == "qwen3.5-122b")

    s_model = mapper.map_model(sglang)
    assert s_model.context_length == 262144
    assert [str(x) for x in s_model.architecture.input_modalities] == ["image"]
    assert s_model.canonical_slug == "litellm/deepseek-v4"

    v_model = mapper.map_model(vllm)
    assert v_model.context_length == 131072
    assert v_model.links.details == ("http://proxy:4000/api/v1/models/litellm/qwen3.5-122b/endpoints")


async def test_partial_failure_keeps_healthy_models():
    routes = _merge(_sglang_routes(), _vllm_routes())
    routes["http://vllm:8000"]["/v1/models"] = DiscoveryHTTPError(500, "boom")
    client = FakeClient(routes)
    registry = DiscoveryAdapterRegistry(client)
    service = DiscoveryService(registry)
    result = await service.discover_many([_sglang_descriptor(), _vllm_descriptor()])
    assert [d.runtime.kind for d in result.discoveries] == ["sglang"]
    assert result.failed_logical_models == {"qwen3.5-122b"}


async def test_failed_model_emits_informative_placeholder():
    routes = _merge(_sglang_routes(), _vllm_routes())
    routes["http://vllm:8000"]["/v1/models"] = DiscoveryHTTPError(500, "boom")
    client = FakeClient(routes)
    registry = DiscoveryAdapterRegistry(client)
    result = await DiscoveryService(registry).discover_many([_sglang_descriptor(), _vllm_descriptor()])
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    placeholder = mapper.map_placeholder(next(iter(result.failed_logical_models)))

    assert placeholder.id == "qwen3.5-122b"
    assert placeholder.canonical_slug == "litellm/qwen3.5-122b"
    assert placeholder.description is not None
    assert "qwen3.5-122b" in placeholder.description
    assert "not properly configured or deployed" in placeholder.description
    # Honest unknowns rather than fabricated semantics.
    assert placeholder.context_length == 0
    assert placeholder.architecture.input_modalities == []
    assert placeholder.architecture.output_modalities == []
    assert placeholder.architecture.modality is None
    # Still serializes through the real OpenRouter contract.
    serialized = placeholder.model_dump_json()
    assert "not properly configured or deployed" in serialized


async def test_internal_paths_not_exposed_in_output():
    client = FakeClient(_merge(_sglang_routes(), _vllm_routes()))
    registry = DiscoveryAdapterRegistry(client)
    result = await DiscoveryService(registry).discover_many([_sglang_descriptor(), _vllm_descriptor()])
    aggregated = ModelAggregator().aggregate_all(result.discoveries)
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    output = mapper.map_model(next(m for m in aggregated if m.logical_model_name == "qwen3.5-122b"))
    serialized = output.model_dump_json()
    assert "vllm:8000" not in serialized
    assert "api_base" not in serialized
    assert "Bearer" not in serialized


async def test_sglang_modality_unknown_is_empty_not_text():
    # No /model_info -> no input modality evidence; must NOT default to "text".
    client = FakeClient(
        {
            "http://sglang:8000": {
                "/v1/models": {"data": [{"id": "unknown-model", "max_model_len": 8192}]},
                "/model_info": DiscoveryHTTPError(404, "no"),
            }
        }
    )
    descriptor = DeploymentDescriptor(
        deployment_id="d",
        logical_model_name="unknown-model",
        provider="sglang",
        model="unknown-model",
        api_base="http://sglang:8000",
        model_info={"discovery_runtime": "sglang"},
    )
    registry = DiscoveryAdapterRegistry(client)
    result = await DiscoveryService(registry).discover_many([descriptor])
    aggregated = ModelAggregator().aggregate_all(result.discoveries)
    mapper = OpenRouterModelMapper(details_base_url="http://proxy:4000")
    model = mapper.map_model(aggregated[0])
    # Honest "unknown" representation: empty arrays, None modality.
    assert model.architecture.input_modalities == []
    assert model.architecture.output_modalities == []
    assert model.architecture.modality is None
