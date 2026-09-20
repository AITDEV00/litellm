"""Demo: what GET /api/v1/models returns for sglang, vllm, custom, and broken.

Drives the real OpenRouterModelsService.list_models end-to-end using a fake
HTTP client in place of upstream runtimes, so no live proxy or network is
needed. The output is exactly the JSON the /api/v1/models route would emit.

Run (from repo root):
    .venv/bin/python oicm-litellm-layer/examples/openrouter/demo_openrouter_models.py
"""

# Demo script: the whole point is to print the JSON, so allow T201.
# ruff: noqa: T201

from __future__ import annotations

import asyncio
import json
import logging
from typing import cast

# litellm prints registry/provider notices on import; quiet them for a clean demo.
logging.disable(logging.CRITICAL)

from openrouter.components.model import Model

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.openrouter_compat.discovery.resolver import (
    DeploymentDescriptor,
    DeploymentResolver,
)
from litellm.proxy.openrouter_compat.models_service import OpenRouterModelsService
from litellm.proxy.openrouter_compat.transport.client import DiscoveryTarget
from litellm.proxy.openrouter_compat.transport.errors import DiscoveryHTTPError

RouteFixture = dict[str, object] | DiscoveryHTTPError

class FakeHttpClient:
    """Local stand-in for the discovery HTTP client (duck-typed)."""

    def __init__(self, routes: dict[tuple[str, str], RouteFixture]) -> None:
        self._routes = dict(routes)

    async def get_json(self, target: DiscoveryTarget, path: str) -> dict[str, object]:
        value = self._routes.get((target.api_base, path))
        if isinstance(value, DiscoveryHTTPError):
            raise value
        if value is None:
            raise DiscoveryHTTPError(404, f"no route {path}")
        return value

    async def aclose(self) -> None:
        return None


class FakeResolver(DeploymentResolver):
    """Replace the router-backed resolver so no live Router is needed."""

    def __init__(self, descriptors: list[DeploymentDescriptor]) -> None:
        super().__init__(llm_router=None)
        self._descriptors = descriptors

    async def resolve_for_request(self, **kwargs: object) -> list[DeploymentDescriptor]:
        return self._descriptors


def _descriptors() -> list[DeploymentDescriptor]:
    return [
        # vLLM: /v1/models + /openapi.json
        DeploymentDescriptor(
            deployment_id="vllm-dep",
            logical_model_name="qwen3.5-122b",
            provider="vllm",
            model="qwen3.5-122b",
            api_base="http://vllm:8000",
            model_info={"discovery_runtime": "vllm"},
        ),
        # SGLang: /v1/models + /model_info + /openapi.json
        DeploymentDescriptor(
            deployment_id="sglang-dep",
            logical_model_name="deepseek-v4",
            provider="sglang",
            model="deepseek-v4",
            api_base="http://sglang:8000",
            model_info={"discovery_runtime": "sglang"},
        ),
        # Custom OpenAI-compatible runtime, not sglang/vllm: only /v1/models
        DeploymentDescriptor(
            deployment_id="custom-dep",
            logical_model_name="my-custom-model",
            provider="mycorp",
            model="my-custom-model",
            api_base="http://custom:9000",
        ),
        # Broken runtime: probe fails -> nothing discovered -> placeholder
        DeploymentDescriptor(
            deployment_id="broken-dep",
            logical_model_name="broken-model",
            provider="broken",
            model="broken-model",
            api_base="http://broken:8000",
        ),
    ]


def _routes() -> dict[tuple[str, str], RouteFixture]:
    return {
        ("http://vllm:8000", "/v1/models"): {
            "data": [{"id": "qwen3.5-122b", "max_model_len": 131072}]
        },
        ("http://vllm:8000", "/openapi.json"): {
            "paths": {
                "/v1/chat/completions": {"post": {}},
                "/v1/embeddings": {"post": {}},
                "/v1/rerank": {"post": {}},
            },
            "components": {"schemas": {}},
        },
        ("http://sglang:8000", "/v1/models"): {
            "data": [{"id": "deepseek-v4", "max_model_len": 262144}]
        },
        ("http://sglang:8000", "/model_info"): {
            "is_generation": True,
            "has_image_understanding": False,
            "has_audio_understanding": False,
            "model_type": "deepseek_v4",
            "architectures": ["DeepseekV4ForCausalLM"],
        },
        ("http://sglang:8000", "/openapi.json"): {
            "paths": {
                "/v1/chat/completions": {"post": {}},
                "/v1/embeddings": {"post": {}},
            },
            "components": {"schemas": {}},
        },
        ("http://custom:9000", "/v1/models"): {
            "data": [{"id": "my-custom-model", "max_model_len": 32768}]
        },
        ("http://broken:8000", "/v1/models"): DiscoveryHTTPError(500, "boom"),
    }


async def main() -> None:
    http = FakeHttpClient(_routes())
    service = OpenRouterModelsService(
        llm_router=None,
        details_base_url="http://localhost:4000",
        http_client=http,  # pyright: ignore[reportArgumentType]  # FakeHttpClient duck-types
    )
    # Inject a resolver so we don't need a live Router.
    service._resolver = FakeResolver(_descriptors())  # pyright: ignore[reportPrivateUsage]  # demo-only: no public setter

    user = UserAPIKeyAuth(user_id="demo-user", user_role=LitellmUserRoles.PROXY_ADMIN)
    result = await service.list_models(
        user_api_key_dict=user,
        general_settings={},
        prisma_client=None,
        proxy_logging_obj=None,
        user_api_key_cache=None,
    )
    await service.aclose()

    # FastAPI serializes the pydantic Model objects to JSON; mirror that here.
    data = cast(list[Model], result["data"])
    body = {
        "data": [m.model_dump(mode="json") for m in data],
        "total_count": result["total_count"],
        "links": result["links"],
    }
    print(json.dumps(body, indent=2))


async def _jsonable(value: object) -> object:
    """Mirror FastAPI's jsonable_encoder: sets -> lists (model_dump already
    handles the pydantic objects)."""
    if isinstance(value, set):
        return sorted(value, key=str)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]  # set elements are JSON-scalars
    if isinstance(value, dict):
        return {k: await _jsonable(v) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]  # generic recursion
    if isinstance(value, list):
        return [await _jsonable(v) for v in value]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]  # generic recursion
    return value


async def main_endpoints() -> None:
    """Show the per-model endpoints route output for the vLLM deployment."""
    http = FakeHttpClient(_routes())
    service = OpenRouterModelsService(
        llm_router=None,
        details_base_url="http://localhost:4000",
        http_client=http,  # pyright: ignore[reportArgumentType]  # FakeHttpClient duck-types
    )
    service._resolver = FakeResolver(_descriptors())  # pyright: ignore[reportPrivateUsage]  # demo-only: no public setter

    user = UserAPIKeyAuth(user_id="demo-user", user_role=LitellmUserRoles.PROXY_ADMIN)
    result = await service.get_model_endpoints(
        author="litellm",
        slug="qwen3.5-122b",
        user_api_key_dict=user,
        general_settings={},
        prisma_client=None,
        proxy_logging_obj=None,
        user_api_key_cache=None,
        team_id=None,
    )
    await service.aclose()
    print(json.dumps(await _jsonable(result), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
    print("\n===== GET /api/v1/models/litellm/qwen3.5-122b/endpoints =====\n")
    asyncio.run(main_endpoints())