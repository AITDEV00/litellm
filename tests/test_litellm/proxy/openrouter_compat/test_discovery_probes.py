"""Tests for reusable upstream probes (design §8, §37.2).

Covers OpenAIModelsProbe, SGLangModelInfoProbe fallback, and OpenAPISchemaProbe
behaviour (available / disabled / ref resolution).
"""

from __future__ import annotations

from litellm.proxy.openrouter_compat.discovery.probes.openai_models import (
    OpenAIModelsProbe,
)
from litellm.proxy.openrouter_compat.discovery.probes.openapi import (
    OpenAPIInspector,
    OpenAPISchemaProbe,
)
from litellm.proxy.openrouter_compat.discovery.probes.sglang_model_info import (
    SGLangModelInfoProbe,
)
from litellm.proxy.openrouter_compat.transport.client import DiscoveryTarget
from litellm.proxy.openrouter_compat.transport.errors import (
    DiscoveryHTTPError,
    DiscoveryTimeout,
)

TARGET = DiscoveryTarget(deployment_id="dep-1", api_base="http://runtime:8000", auth_headers={})


class FakeClient:
    """Deterministic fake HTTP client mapping path -> (payload | exception)."""

    def __init__(self, routes: dict[str, object]) -> None:
        self._routes = dict(routes)
        self.calls: list[tuple[str, str]] = []

    async def get_json(self, target: DiscoveryTarget, path: str) -> dict[str, object]:
        self.calls.append((target.api_base, path))
        value = self._routes.get(path)
        if isinstance(value, Exception):
            raise value
        return dict(value)  # type: ignore[arg-type]


def _valid_models_payload() -> dict[str, object]:
    return {
        "object": "list",
        "data": [
            {"id": "m1", "max_model_len": 65536},
            {"id": "m2"},
        ],
    }


async def test_openai_models_probe_success():
    probe = OpenAIModelsProbe(FakeClient({"/v1/models": _valid_models_payload()}))
    result = await probe.run(TARGET)
    assert result.success
    assert result.data is not None
    assert [c.id for c in result.data] == ["m1", "m2"]
    assert result.data[0].max_model_len == 65536


async def test_openai_models_probe_schema_error_when_not_list():
    probe = OpenAIModelsProbe(FakeClient({"/v1/models": {"object": "list", "data": {"not": "list"}}}))
    result = await probe.run(TARGET)
    assert not result.success
    assert result.error_category == "DiscoverySchemaError"


async def test_openai_models_probe_invalid_card_schema():
    probe = OpenAIModelsProbe(FakeClient({"/v1/models": {"data": [{"no_id": True}]}}))
    result = await probe.run(TARGET)
    assert not result.success
    assert result.error_category == "DiscoverySchemaError"


async def test_openai_models_probe_timeout_maps_category():
    probe = OpenAIModelsProbe(FakeClient({"/v1/models": DiscoveryTimeout("t")}))
    result = await probe.run(TARGET)
    assert not result.success
    assert result.error_category == "DiscoveryTimeout"


async def test_sglang_probe_prefers_model_info():
    payload = {"model_type": "deepseek_v4", "is_generation": True}
    client = FakeClient({"/model_info": payload})
    probe = SGLangModelInfoProbe(client)
    result = await probe.run(TARGET)
    assert result.success
    assert result.data is not None
    assert result.data.model_type == "deepseek_v4"
    assert client.calls == [(TARGET.api_base, "/model_info")]


async def test_sglang_probe_falls_back_to_get_model_info():
    payload = {"model_type": "deepseek_v4"}
    client = FakeClient(
        {
            "/model_info": DiscoveryHTTPError(404, "no"),
            "/get_model_info": payload,
        }
    )
    probe = SGLangModelInfoProbe(client)
    result = await probe.run(TARGET)
    assert result.success
    assert result.data is not None
    assert [p for _, p in client.calls] == ["/model_info", "/get_model_info"]


async def test_sglang_probe_all_unavailable_returns_error():
    client = FakeClient(
        {
            "/model_info": DiscoveryHTTPError(404, "a"),
            "/get_model_info": DiscoveryHTTPError(404, "b"),
        }
    )
    probe = SGLangModelInfoProbe(client)
    result = await probe.run(TARGET)
    assert not result.success
    assert result.error_category == "DiscoveryHTTPError"


async def test_openapi_probe_available():
    openapi_doc = {
        "paths": {"/v1/chat/completions": {"post": {}}},
        "components": {"schemas": {}},
    }
    probe = OpenAPISchemaProbe(FakeClient({"/openapi.json": openapi_doc}))
    result = await probe.run(TARGET)
    assert result.success
    assert result.data is not None
    assert result.data.has_path("/v1/chat/completions")
    assert result.data.has_operation("/v1/chat/completions", "post")


async def test_openapi_probe_disabled_404():
    probe = OpenAPISchemaProbe(FakeClient({"/openapi.json": DiscoveryHTTPError(404, "disabled")}))
    result = await probe.run(TARGET)
    assert not result.success
    assert result.error_category == "DiscoveryHTTPError"


def test_openapi_inspector_route_inference():
    inspector = OpenAPIInspector(
        {
            "paths": {
                "/v1/chat/completions": {"post": {"operationId": "chat"}},
                "/v1/embeddings": {"post": {}},
            },
            "components": {"schemas": {}},
        }
    )
    assert inspector.has_path("/v1/embeddings")
    assert inspector.route_paths() == {"/v1/chat/completions", "/v1/embeddings"}


def test_openapi_inspector_ref_resolution():
    inspector = OpenAPIInspector(
        {
            "paths": {
                "/v1/chat/completions": {
                    "post": {
                        "requestBody": {
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Req"}}}
                        }
                    }
                }
            },
            "components": {"schemas": {"Req": {"tools": {"type": "boolean"}}}},
        }
    )
    assert inspector.request_schema_mentions("/v1/chat/completions", "tools")


def test_openapi_inspector_schema_mentions_cycle_guard():
    # A <-> B self-referencing schemas: traversal must terminate.
    inspector = OpenAPIInspector(
        {
            "paths": {
                "/v1/x": {
                    "post": {
                        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/A"}}}}
                    }
                }
            },
            "components": {
                "schemas": {
                    "A": {"b": {"$ref": "#/components/schemas/B"}},
                    "B": {"a": {"$ref": "#/components/schemas/A"}},
                }
            },
        }
    )
    assert inspector.request_schema_mentions("/v1/x", "missing") is False
    assert inspector.request_schema_mentions("/v1/does-not-exist", "missing") is False
