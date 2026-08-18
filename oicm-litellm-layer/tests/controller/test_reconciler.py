from unittest.mock import AsyncMock, MagicMock

import pytest

from controller.models import OicmModel
from controller.reconciler import SyncPlan, SyncReconciler


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
    litellm_by_key = {"tts-uuid": [_make_litellm_entry("litellm-id-1", model_name="omnivoice/omnivoice")]}

    plan = await reconciler.compute_plan(k8s_models, litellm_by_key)

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
    litellm_by_key = {"chat-uuid": [_make_litellm_entry("litellm-id-2")]}

    plan = await reconciler.compute_plan(k8s_models, litellm_by_key)

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
    litellm_by_key = {}

    plan = await reconciler.compute_plan(k8s_models, litellm_by_key)

    assert len(plan.registers) == 1
    registered_model, _ = plan.registers[0]
    assert registered_model.mode == "text_to_speech"


def _make_multi_model(uuid, model_id, provider="hosted_vllm", mode="chat"):
    return OicmModel(
        uuid=uuid,
        model_id=model_id,
        model_name=model_id,
        namespace="adeo",
        ready_replicas=1,
        total_replicas=1,
        mode=mode,
        provider=provider,
    )


@pytest.mark.asyncio
async def test_register_multiple_models_per_deployment():
    """One deployment advertising N models must register N model records."""
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    reconciler = SyncReconciler(MagicMock(), pricing)

    dep_uuid = "doc-uuid"
    k8s_models = {
        f"{dep_uuid}::PP-DocLayoutV3": _make_multi_model(dep_uuid, "PP-DocLayoutV3"),
        f"{dep_uuid}::PP-StructureV3": _make_multi_model(dep_uuid, "PP-StructureV3"),
    }
    litellm_by_key = {}

    plan = await reconciler.compute_plan(k8s_models, litellm_by_key)

    assert len(plan.registers) == 2
    registered_model_ids = {m.model_id for m, _ in plan.registers}
    assert registered_model_ids == {"PP-DocLayoutV3", "PP-StructureV3"}
    # Both share the same api_base (same deployment)
    bases = {m.api_base for m, _ in plan.registers}
    assert len(bases) == 1


@pytest.mark.asyncio
async def test_multiple_models_patched_independently():
    """Two models of one deployment reconcile without collapsing to a single model."""
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    reconciler = SyncReconciler(MagicMock(), pricing)

    dep_uuid = "doc-uuid"
    k8s_models = {
        f"{dep_uuid}::PP-DocLayoutV3": _make_multi_model(dep_uuid, "PP-DocLayoutV3"),
        f"{dep_uuid}::PP-StructureV3": _make_multi_model(dep_uuid, "PP-StructureV3"),
    }
    litellm_by_key = {
        f"{dep_uuid}::PP-DocLayoutV3": [_make_litellm_entry("id-layout", model_name="PP-DocLayoutV3")],
        f"{dep_uuid}::PP-StructureV3": [_make_litellm_entry("id-struct", model_name="PP-StructureV3")],
    }

    plan = await reconciler.compute_plan(k8s_models, litellm_by_key)

    assert len(plan.registers) == 0
    assert len(plan.patches) == 2
    patched_ids = {p[0] for p in plan.patches}
    assert patched_ids == {"id-layout", "id-struct"}


@pytest.mark.asyncio
async def test_execute_keys_state_by_composite_key():
    """execute() must not collapse a multi-model deployment into one state entry.

    Regression: execute() used to key plan.new_state / plan.new_id_map by the bare
    model.uuid. For a deployment hosting N models (N composite keys sharing one
    uuid), the last registered model would overwrite the others, leaving the
    controller tracking only one of the N models after a full sync.
    """
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    litellm = MagicMock()
    # batch() returns (deleted, [registered ids in order], patched)
    litellm.batch = AsyncMock(return_value=(0, ["id-layout", "id-struct"], 0))
    reconciler = SyncReconciler(litellm, pricing)

    dep_uuid = "doc-uuid"
    model_a = _make_multi_model(dep_uuid, "PP-DocLayoutV3")
    model_b = _make_multi_model(dep_uuid, "PP-StructureV3")
    plan = SyncPlan()
    plan.registers = [(model_a, None), (model_b, None)]

    deleted, registered, patched = await reconciler.execute(plan)

    assert (deleted, registered, patched) == (0, 2, 0)
    assert set(plan.new_state.keys()) == {
        f"{dep_uuid}::PP-DocLayoutV3",
        f"{dep_uuid}::PP-StructureV3",
    }
    assert plan.new_id_map == {
        f"{dep_uuid}::PP-DocLayoutV3": "id-layout",
        f"{dep_uuid}::PP-StructureV3": "id-struct",
    }


@pytest.mark.asyncio
async def test_execute_aligns_ids_when_a_register_fails():
    """execute() must align registered ids to their model by position even when
    a mid-batch register fails (None placeholder).

    Regression: batch() used to FILTER None out of registered_ids before
    returning. execute() then zipped that shorter list against ALL registers,
    so a failure at index 1 would shift register[2]'s id onto register[1] and
    never assign register[2] an id.
    """
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    litellm = MagicMock()
    # batch() now returns the unfiltered, position-preserving list (None = failed).
    litellm.batch = AsyncMock(return_value=(0, ["id-a", None, "id-c"], 0))
    reconciler = SyncReconciler(litellm, pricing)

    dep_uuid = "doc-uuid"
    models = [
        _make_multi_model(dep_uuid, "m-a"),
        _make_multi_model(dep_uuid, "m-b"),
        _make_multi_model(dep_uuid, "m-c"),
    ]
    plan = SyncPlan()
    plan.registers = [(m, None) for m in models]

    deleted, registered, patched = await reconciler.execute(plan)

    assert (deleted, registered, patched) == (0, 3, 0)
    assert plan.new_id_map == {
        f"{dep_uuid}::m-a": "id-a",
        f"{dep_uuid}::m-c": "id-c",
    }
    # The failed register (m-b) must NOT be in state (no id assigned), and its
    # neighbors must keep their correct ids (no shifting).
    assert f"{dep_uuid}::m-b" not in plan.new_state
    assert plan.new_state[f"{dep_uuid}::m-a"].model_id == "m-a"
    assert plan.new_state[f"{dep_uuid}::m-c"].model_id == "m-c"
