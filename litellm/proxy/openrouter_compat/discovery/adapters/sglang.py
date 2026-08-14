"""SGLang discovery adapter. Probes /v1/models + /model_info + optional openapi."""

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
from litellm.proxy.openrouter_compat.discovery.probes.sglang_model_info import (
    SGLangModelInfoProbe,
)
from litellm.proxy.openrouter_compat.domain.architecture import ModelArchitecture
from litellm.proxy.openrouter_compat.domain.capabilities import (
    ModelCapabilities,
)
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.domain.provenance import FactSource
from litellm.proxy.openrouter_compat.transport.client import DiscoveryTarget
from litellm.proxy.openrouter_compat.transport.dto import SGLangModelInfo


class SGLangDiscoveryAdapter(OpenAICompatibleRuntimeAdapter):
    runtime_kind = "sglang"

    def __init__(
        self,
        models_probe: OpenAIModelsProbe,
        model_info_probe: SGLangModelInfoProbe,
        openapi_probe: OpenAPISchemaProbe,
    ) -> None:
        super().__init__(models_probe)
        self._model_info_probe = model_info_probe
        self._openapi_probe = openapi_probe

    async def discover(
        self, target: DiscoveryTarget, logical_model_name: str
    ) -> list[DiscoveredDeploymentModel]:
        models = await super().discover(target, logical_model_name)
        info_result = await self._model_info_probe.run(target)
        if info_result.success and info_result.data is not None:
            models = [
                self._apply_model_info(model, info_result.data)
                for model in models
            ]
        openapi_result = await self._openapi_probe.run(target)
        if openapi_result.success and openapi_result.data is not None:
            models = [
                self._apply_openapi(model, openapi_result.data)
                for model in models
            ]
        return models

    def _apply_model_info(
        self,
        model: DiscoveredDeploymentModel,
        sglang_info: SGLangModelInfo,
    ) -> DiscoveredDeploymentModel:
        input_modalities: set[str] = set()
        if sglang_info.has_image_understanding:
            input_modalities.add("image")
        if sglang_info.has_audio_understanding:
            input_modalities.add("audio")
        capabilities = ModelCapabilities(
            input_modalities=input_modalities or None,
            output_modalities={"text"} if sglang_info.is_generation else None,
        )
        architecture = ModelArchitecture(
            model_type=sglang_info.model_type,
            architectures=list(sglang_info.architectures or []),
        )
        provenance = model.provenance.model_copy(
            update={
                "facts": {
                    **model.provenance.facts,
                    "architecture.model_type": FactSource(
                        probe="sglang_model_info", field="model_type"
                    ),
                    "capabilities.input_modalities.image": FactSource(
                        probe="sglang_model_info",
                        field="has_image_understanding",
                    ),
                }
            }
        )
        return model.model_copy(
            update={
                "capabilities": capabilities,
                "architecture": architecture,
                "provenance": provenance,
            }
        )

    def _apply_openapi(
        self,
        model: DiscoveredDeploymentModel,
        inspector: OpenAPIInspector,
    ) -> DiscoveredDeploymentModel:
        api = model.api_capabilities.model_copy(update={})
        api.chat_completions = inspector.has_operation("/v1/chat/completions", "post")
        api.completions = inspector.has_operation("/v1/completions", "post")
        api.embeddings = inspector.has_operation("/v1/embeddings", "post")
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