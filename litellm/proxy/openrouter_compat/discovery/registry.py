"""Adapter registry. Maps a LiteLLM deployment to the right discovery adapter.

Runtime selection precedence (per design §11):
1. explicit ``model_info.discovery_runtime`` override
2. known provider hint (``sglang`` / ``vllm`` custom provider)
3. generic OpenAI-compatible fallback
"""

from __future__ import annotations

from litellm.proxy.openrouter_compat.discovery.adapters.openai_compatible import (
    OpenAICompatibleRuntimeAdapter,
)
from litellm.proxy.openrouter_compat.discovery.adapters.sglang import (
    SGLangDiscoveryAdapter,
)
from litellm.proxy.openrouter_compat.discovery.adapters.vllm import VLLMDiscoveryAdapter
from litellm.proxy.openrouter_compat.discovery.resolver import DeploymentDescriptor
from litellm.proxy.openrouter_compat.transport.client import DiscoveryHTTPClient
from litellm.proxy.openrouter_compat.transport.errors import UnsupportedRuntime

_SUPPORTED_RUNTIMES: frozenset[str] = frozenset(
    {"openai-compatible", "vllm", "sglang"}
)


class DiscoveryAdapterRegistry:
    """Registry that lazily builds adapters on demand (not runtime schema codegen)."""

    def __init__(self, http_client: DiscoveryHTTPClient) -> None:
        self._http_client = http_client
        # Runtime-kind -> adapter instance cache keyed by kind so probes reuse one client.
        self._adapters: dict[str, OpenAICompatibleRuntimeAdapter] = {}

    def resolve(
        self, descriptor: DeploymentDescriptor
    ) -> OpenAICompatibleRuntimeAdapter:
        runtime_kind = self._detect_runtime_kind(descriptor)
        if runtime_kind not in _SUPPORTED_RUNTIMES:
            raise UnsupportedRuntime(f"unsupported discovery runtime: {runtime_kind}")
        cached = self._adapters.get(runtime_kind)
        if cached is not None:
            return cached
        adapter = self._build(runtime_kind)
        self._adapters[runtime_kind] = adapter
        return adapter

    @staticmethod
    def _detect_runtime_kind(descriptor: DeploymentDescriptor) -> str:
        override = descriptor.model_info.get("discovery_runtime")
        if isinstance(override, str) and override:
            return override
        provider = descriptor.provider
        if provider == "sglang":
            return "sglang"
        if provider == "vllm":
            return "vllm"
        return "openai-compatible"

    def _build(self, runtime_kind: str) -> OpenAICompatibleRuntimeAdapter:
        from litellm.proxy.openrouter_compat.discovery.probes.openai_models import (
            OpenAIModelsProbe,
        )
        from litellm.proxy.openrouter_compat.discovery.probes.openapi import (
            OpenAPISchemaProbe,
        )
        from litellm.proxy.openrouter_compat.discovery.probes.sglang_model_info import (
            SGLangModelInfoProbe,
        )

        models_probe = OpenAIModelsProbe(self._http_client)
        if runtime_kind == "vllm":
            return VLLMDiscoveryAdapter(
                models_probe=models_probe,
                openapi_probe=OpenAPISchemaProbe(self._http_client),
            )
        if runtime_kind == "sglang":
            return SGLangDiscoveryAdapter(
                models_probe=models_probe,
                model_info_probe=SGLangModelInfoProbe(self._http_client),
                openapi_probe=OpenAPISchemaProbe(self._http_client),
            )
        return OpenAICompatibleRuntimeAdapter(models_probe=models_probe)