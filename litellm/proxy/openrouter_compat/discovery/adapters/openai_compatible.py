"""OpenAI-compatible runtime adapter (base for vLLM, SGLang, and generic runtimes)."""

from __future__ import annotations

from litellm.proxy.openrouter_compat.discovery.base import BaseDiscoveryAdapter
from litellm.proxy.openrouter_compat.discovery.probes.openai_models import (
    OpenAIModelsProbe,
)
from litellm.proxy.openrouter_compat.discovery.probes.openapi import OpenAPIInspector
from litellm.proxy.openrouter_compat.domain.architecture import ModelArchitecture
from litellm.proxy.openrouter_compat.domain.capabilities import (
    ApiCapabilities,
    ModelCapabilities,
)
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.domain.identity import ModelIdentity
from litellm.proxy.openrouter_compat.domain.limits import ModelLimits
from litellm.proxy.openrouter_compat.domain.provenance import (
    FactSource,
    ModelProvenance,
    RuntimeInfo,
)
from litellm.proxy.openrouter_compat.transport.client import (
    DiscoveryTarget,
    fingerprint,
)
from litellm.proxy.openrouter_compat.transport.dto import RuntimeModelCard


class OpenAICompatibleRuntimeAdapter(BaseDiscoveryAdapter[RuntimeModelCard]):
    runtime_kind = "openai-compatible"

    def __init__(self, models_probe: OpenAIModelsProbe) -> None:
        self._models_probe = models_probe

    async def discover_models(self, target: DiscoveryTarget) -> list[RuntimeModelCard]:
        result = await self._models_probe.run(target)
        if not result.success or result.data is None:
            raise RuntimeError(f"openai_models probe failed: {result.error_category}")
        return result.data

    def normalize_model(
        self,
        target: DiscoveryTarget,
        logical_model_name: str,
        raw: RuntimeModelCard,
    ) -> DiscoveredDeploymentModel:
        identity = ModelIdentity(
            logical_model_name=logical_model_name,
            upstream_model_id=raw.id,
            root=raw.root,
            parent=raw.parent,
            created=raw.created,
        )
        limits = ModelLimits(context_length=raw.max_model_len)
        provenance = ModelProvenance(
            facts={
                "identity.upstream_model_id": FactSource(probe="openai_models", field="id"),
                "limits.context_length": FactSource(probe="openai_models", field="max_model_len"),
            }
        )
        runtime = RuntimeInfo(
            kind=self.runtime_kind,
            deployment_id=target.deployment_id,
            api_base_fingerprint=fingerprint(target.api_base),
        )
        return DiscoveredDeploymentModel(
            identity=identity,
            limits=limits,
            architecture=ModelArchitecture(),
            capabilities=ModelCapabilities(),
            api_capabilities=ApiCapabilities(),
            runtime=runtime,
            provenance=provenance,
        )

    def _apply_openapi(
        self,
        model: DiscoveredDeploymentModel,
        inspector: OpenAPIInspector,
    ) -> DiscoveredDeploymentModel:
        """Fill API capabilities from the OpenAPI route surface (shared by runtimes)."""
        api = model.api_capabilities.model_copy(update={})
        api.chat_completions = inspector.has_operation("/v1/chat/completions", "post")
        api.completions = inspector.has_operation("/v1/completions", "post")
        api.embeddings = inspector.has_operation("/v1/embeddings", "post")
        api.transcription = inspector.has_operation("/v1/audio/transcriptions", "post")
        api.speech = inspector.has_operation("/v1/audio/speech", "post")
        api.rerank = inspector.has_operation("/v1/rerank", "post")
        api.routes = inspector.route_paths()
        provenance = model.provenance.model_copy(
            update={
                "facts": {
                    **model.provenance.facts,
                    "api_capabilities.chat_completions": FactSource(probe="openapi", field="/v1/chat/completions"),
                }
            }
        )
        return model.model_copy(update={"api_capabilities": api, "provenance": provenance})
