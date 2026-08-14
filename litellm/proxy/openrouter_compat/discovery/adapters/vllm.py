"""vLLM discovery adapter. Probes /v1/models + optional /openapi.json."""

from __future__ import annotations

from litellm.proxy.openrouter_compat.discovery.adapters.openai_compatible import (
    OpenAICompatibleRuntimeAdapter,
)
from litellm.proxy.openrouter_compat.discovery.probes.openai_models import (
    OpenAIModelsProbe,
)
from litellm.proxy.openrouter_compat.discovery.probes.openapi import (
    OpenAPIInspector,
    OpenAPISchemaProbe,
)
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.domain.provenance import FactSource
from litellm.proxy.openrouter_compat.transport.client import DiscoveryTarget


class VLLMDiscoveryAdapter(OpenAICompatibleRuntimeAdapter):
    runtime_kind = "vllm"

    def __init__(
        self,
        models_probe: OpenAIModelsProbe,
        openapi_probe: OpenAPISchemaProbe,
    ) -> None:
        super().__init__(models_probe)
        self._openapi_probe = openapi_probe

    async def discover(
        self, target: DiscoveryTarget, logical_model_name: str
    ) -> list[DiscoveredDeploymentModel]:
        models = await super().discover(target, logical_model_name)
        result = await self._openapi_probe.run(target)
        if result.success and result.data is not None:
            return [
                self._apply_openapi(model, result.data)
                for model in models
            ]
        return models

    def _apply_openapi(
        self,
        model: DiscoveredDeploymentModel,
        inspector: OpenAPIInspector,
    ) -> DiscoveredDeploymentModel:
        api = model.api_capabilities.model_copy(deep=True)
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
                    "api_capabilities.chat_completions": FactSource(
                        probe="openapi", field="/v1/chat/completions"
                    ),
                }
            }
        )
        return model.model_copy(
            update={"api_capabilities": api, "provenance": provenance}
        )