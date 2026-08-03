from unittest.mock import AsyncMock, MagicMock

import pytest

from controller.models import OicmModel
from controller.reconciler import SyncReconciler


def _make_model(uuid, mode="chat", provider="hosted_vllm", model_id="test-model"):
    return OicmModel(
        uuid=uuid,
        model_id=model_id,
        model_name=f"{provider}/{model_id}",
        namespace="adeo",
        ready_replicas=1,
        total_replicas=1,
        mode=mode,
        provider=provider,
    )


def _make_litellm_entry(model_id, model_name="hosted_vllm/test-model", mode="chat"):
    return {
        "model_id": model_id,
        "model_name": model_name,
        "litellm_params": {"model": model_name, "api_base": "http://old:8080/v1"},
        "model_info": {"id": model_id, "mode": mode, "oicm_uuid": "abc"},
    }


@pytest.mark.asyncio
async def test_patch_includes_corrected_mode_for_tts_model():
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    reconciler = SyncReconciler(MagicMock(), pricing)

    model = _make_model("tts-uuid", mode="text_to_speech", provider="omnivoice", model_id="omnivoice")
    k8s_models = {"tts-uuid": model}
    litellm_by_uuid = {"tts-uuid": [_make_litellm_entry("litellm-id-1", model_name="omnivoice/omnivoice")]}

    plan = await reconciler.compute_plan(k8s_models, litellm_by_uuid)

    assert len(plan.patches) == 1
    patch_id, patch_params, patch_model_info = plan.patches[0]
    assert patch_id == "litellm-id-1"
    assert patch_params["model"] == "omnivoice/omnivoice"
    assert patch_model_info is not None
    assert patch_model_info["mode"] == "audio_speech"


@pytest.mark.asyncio
async def test_patch_mode_unchanged_for_chat_model():
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    reconciler = SyncReconciler(MagicMock(), pricing)

    model = _make_model("chat-uuid", mode="chat")
    k8s_models = {"chat-uuid": model}
    litellm_by_uuid = {"chat-uuid": [_make_litellm_entry("litellm-id-2")]}

    plan = await reconciler.compute_plan(k8s_models, litellm_by_uuid)

    assert len(plan.patches) == 1
    _, _, patch_model_info = plan.patches[0]
    assert patch_model_info["mode"] == "chat"


@pytest.mark.asyncio
async def test_register_uses_corrected_mode_for_tts_model():
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    reconciler = SyncReconciler(MagicMock(), pricing)

    model = _make_model("new-tts-uuid", mode="text_to_speech", provider="omnivoice", model_id="omnivoice")
    k8s_models = {"new-tts-uuid": model}
    litellm_by_uuid = {}

    plan = await reconciler.compute_plan(k8s_models, litellm_by_uuid)

    assert len(plan.registers) == 1
    registered_model, _ = plan.registers[0]
    assert registered_model.mode == "text_to_speech"
