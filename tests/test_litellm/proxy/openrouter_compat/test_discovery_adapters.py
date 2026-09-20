"""Tests for discovery adapters (design §9-10, §37.3).

Covers vLLM (max_model_len -> context_length, internal root, OpenAPI API
capability merge) and SGLang (/model_info modality/architecture enrichment).
"""

from __future__ import annotations

from litellm.proxy.openrouter_compat.discovery.adapters.sglang import (
    SGLangDiscoveryAdapter,
)
from litellm.proxy.openrouter_compat.discovery.adapters.vllm import VLLMDiscoveryAdapter
from litellm.proxy.openrouter_compat.discovery.probes.openai_models import (
    OpenAIModelsProbe,
)
from litellm.proxy.openrouter_compat.discovery.probes.openapi import (
    OpenAPISchemaProbe,
)
from litellm.proxy.openrouter_compat.discovery.probes.sglang_model_info import (
    SGLangModelInfoProbe,
)
from litellm.proxy.openrouter_compat.transport.client import DiscoveryTarget
from litellm.proxy.openrouter_compat.transport.errors import DiscoveryHTTPError

TARGET = DiscoveryTarget(deployment_id="dep-1", api_base="http://runtime:8000", auth_headers={})


class FakeClient:
    def __init__(self, routes: dict[str, object]) -> None:
        self._routes = dict(routes)

    async def get_json(self, target: DiscoveryTarget, path: str) -> dict[str, object]:
        value = self._routes.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise DiscoveryHTTPError(404, f"no route {path}")
        return dict(value)  # type: ignore[arg-type]


def _vllm_models_payload() -> dict[str, object]:
    return {
        "data": [
            {
                "id": "qwen3.5-122b",
                "root": "models-fs-root",
                "parent": None,
                "created": 1720000001,
                "max_model_len": 131072,
            }
        ]
    }


def _sglang_models_payload() -> dict[str, object]:
    return {"data": [{"id": "deepseek-v4", "max_model_len": 262144}]}


def _vllm_adapter(client: FakeClient) -> VLLMDiscoveryAdapter:
    return VLLMDiscoveryAdapter(OpenAIModelsProbe(client), OpenAPISchemaProbe(client))


def _sglang_adapter(client: FakeClient) -> SGLangDiscoveryAdapter:
    return SGLangDiscoveryAdapter(
        OpenAIModelsProbe(client),
        SGLangModelInfoProbe(client),
        OpenAPISchemaProbe(client),
    )


async def test_vllm_adapter_maps_context_length():
    client = FakeClient({"/v1/models": _vllm_models_payload()})
    models = await _vllm_adapter(client).discover(TARGET, "qwen3.5-122b")
    assert len(models) == 1
    m = models[0]
    assert m.identity.upstream_model_id == "qwen3.5-122b"
    assert m.limits.context_length == 131072
    assert m.runtime.kind == "vllm"
    assert m.runtime.deployment_id == "dep-1"


async def test_vllm_adapter_root_remains_internal():
    client = FakeClient({"/v1/models": _vllm_models_payload()})
    models = await _vllm_adapter(client).discover(TARGET, "qwen3.5-122b")
    assert models[0].identity.root == "models-fs-root"


async def test_vllm_adapter_openapi_merge():
    openapi_doc = {
        "paths": {
            "/v1/chat/completions": {"post": {}},
            "/v1/embeddings": {"post": {}},
        },
        "components": {"schemas": {}},
    }
    client = FakeClient({"/v1/models": _vllm_models_payload(), "/openapi.json": openapi_doc})
    models = await _vllm_adapter(client).discover(TARGET, "qwen3.5-122b")
    api = models[0].api_capabilities
    assert api.chat_completions is True
    assert api.embeddings is True
    assert api.completions is False


async def test_vllm_adapter_openapi_disabled_leaves_unknown():
    client = FakeClient(
        {
            "/v1/models": _vllm_models_payload(),
            "/openapi.json": DiscoveryHTTPError(404, "disabled"),
        }
    )
    models = await _vllm_adapter(client).discover(TARGET, "qwen3.5-122b")
    assert models[0].api_capabilities.chat_completions is None


async def test_sglang_adapter_image_audio_architecture():
    model_info = {
        "is_generation": True,
        "has_image_understanding": True,
        "has_audio_understanding": False,
        "model_type": "deepseek_v4",
        "architectures": ["DeepseekV4ForCausalLM"],
    }
    client = FakeClient({"/v1/models": _sglang_models_payload(), "/model_info": model_info})
    models = await _sglang_adapter(client).discover(TARGET, "deepseek-v4")
    m = models[0]
    assert m.limits.context_length == 262144
    assert m.capabilities.input_modalities == {"image"}
    assert m.capabilities.output_modalities == {"text"}
    assert m.architecture.model_type == "deepseek_v4"
    assert m.architecture.architectures == ["DeepseekV4ForCausalLM"]


async def test_sglang_adapter_false_image_is_meaningful_negative():
    model_info = {
        "is_generation": True,
        "has_image_understanding": False,
        "has_audio_understanding": False,
    }
    client = FakeClient({"/v1/models": _sglang_models_payload(), "/model_info": model_info})
    models = await _sglang_adapter(client).discover(TARGET, "deepseek-v4")
    m = models[0]
    # Both explicitly false -> no input modalities advertised, not "image".
    assert m.capabilities.input_modalities is None
    assert m.capabilities.output_modalities == {"text"}


async def test_sglang_adapter_without_model_info_continues():
    client = FakeClient(
        {
            "/v1/models": _sglang_models_payload(),
            "/model_info": DiscoveryHTTPError(404, "no"),
            "/get_model_info": DiscoveryHTTPError(404, "no"),
        }
    )
    models = await _sglang_adapter(client).discover(TARGET, "deepseek-v4")
    assert len(models) == 1
    assert models[0].capabilities.input_modalities is None


async def test_sglang_openapi_merge_matches_vllm_capability_set():
    # Regression: _apply_openapi is shared with the base adapter, so SGLang
    # must advertise the same API capability surface as vLLM (including
    # transcription/speech/rerank), not a reduced subset.
    openapi_doc = {
        "paths": {
            "/v1/chat/completions": {"post": {}},
            "/v1/embeddings": {"post": {}},
            "/v1/audio/transcriptions": {"post": {}},
            "/v1/audio/speech": {"post": {}},
            "/v1/rerank": {"post": {}},
        },
        "components": {"schemas": {}},
    }
    client = FakeClient(
        {
            "/v1/models": _sglang_models_payload(),
            "/model_info": {"is_generation": True},
            "/openapi.json": openapi_doc,
        }
    )
    models = await _sglang_adapter(client).discover(TARGET, "deepseek-v4")
    api = models[0].api_capabilities
    assert api.chat_completions is True
    assert api.embeddings is True
    assert api.transcription is True
    assert api.speech is True
    assert api.rerank is True
    assert api.routes == set(openapi_doc["paths"])


async def test_openapi_extendable_capability_mapping():
    # The route-to-capability mapping is declarative and extendable: custom
    # /v1 routes (image generation, voice cloning/TTS, video, moderation, etc.)
    # must be discovered from the OpenAPI surface without per-route code.
    openapi_doc = {
        "paths": {
            "/v1/chat/completions": {"post": {}},
            "/v1/images/generations": {"post": {}},
            "/v1/audio/voices": {"get": {}},
            "/v1/audio/speech": {"post": {}},
            "/v1/audio/transcriptions": {"post": {}},
            "/v1/videos": {"post": {}},
            "/v1/moderations": {"post": {}},
            "/v1/rerank": {"post": {}},
        },
        "components": {"schemas": {}},
    }
    client = FakeClient({"/v1/models": _vllm_models_payload(), "/openapi.json": openapi_doc})
    models = await _vllm_adapter(client).discover(TARGET, "qwen3.5-122b")
    api = models[0].api_capabilities
    assert api.chat_completions is True
    assert api.image_generation is True
    assert api.image_edits is False
    assert api.voices is True
    assert api.speech is True
    assert api.transcription is True
    assert api.video_generation is True
    assert api.moderation is True
    assert api.rerank is True
    assert api.classification is False
    assert api.responses is False
    assert api.embeddings is False


async def test_openapi_provenance_covers_all_mapped_capabilities():
    # Regression: provenance must record a fact for every capability in the
    # declarative route-to-capability table, not just chat_completions. Otherwise
    # the table can drift from the recorded evidence without any signal.
    openapi_doc = {
        "paths": {
            "/v1/chat/completions": {"post": {}},
            "/v1/images/generations": {"post": {}},
            "/v1/audio/voices": {"get": {}},
            "/v1/moderations": {"post": {}},
        },
        "components": {"schemas": {}},
    }
    client = FakeClient({"/v1/models": _vllm_models_payload(), "/openapi.json": openapi_doc})
    models = await _vllm_adapter(client).discover(TARGET, "qwen3.5-122b")
    facts = models[0].provenance.facts
    assert facts["api_capabilities.chat_completions"].field == "/v1/chat/completions"
    assert facts["api_capabilities.image_generation"].field == "/v1/images/generations"
    assert facts["api_capabilities.voices"].field == "/v1/audio/voices"
    assert facts["api_capabilities.moderation"].field == "/v1/moderations"
    # Every route in the declarative table must have a matching provenance fact.
    from litellm.proxy.openrouter_compat.discovery.adapters.openai_compatible import (
        ROUTE_TO_API_CAPABILITY,
    )

    assert {f"api_capabilities.{attr}" for attr, _, _ in ROUTE_TO_API_CAPABILITY} <= set(facts)
