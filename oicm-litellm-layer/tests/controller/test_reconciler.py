from unittest.mock import AsyncMock, MagicMock

import pytest

from controller.models import OicmModel, to_litellm_mode
from controller.pricing.models import PricingResult
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


def _entry_matching(model, model_id, costs=None):
    """A gateway entry already holding exactly what compute_plan would patch."""
    params = {"model": f"{model.provider}/{model.model_id}", "api_base": model.api_base}
    if costs:
        params.update(costs)
    info = {"id": model_id, "mode": to_litellm_mode(model.mode)}
    return {
        "model_id": model_id,
        "model_name": model.model_name,
        "litellm_params": params,
        "model_info": info,
    }


def _reconciler_with_costs(costs):
    pricing = MagicMock()
    result = None if costs is None else PricingResult(**costs, matched_keys=(), aggregate_score=0.0, strategy="test")
    pricing.resolve = AsyncMock(return_value=result)
    return SyncReconciler(MagicMock(), pricing)


@pytest.mark.asyncio
async def test_steady_state_issues_no_patch():
    """A cycle that finds nothing new must not rewrite the model.

    Every gateway write reloads every model on every replica and bumps
    model_info.updated_at, which forces a router deployment swap. The controller
    re-probes each cycle, so a cycle that changes nothing has to be a no-op.
    """
    reconciler = _reconciler_with_costs(None)
    model = _make_model("stable-uuid")
    k8s_models = {"stable-uuid": model}
    litellm_by_key = {"stable-uuid": [_entry_matching(model, "id-1")]}

    plan = await reconciler.compute_plan(k8s_models, litellm_by_key)

    assert plan.patches == []
    assert plan.deletes == []
    assert plan.registers == []
    # The model must still be tracked, or the next cycle would re-register it.
    assert plan.new_state == {"stable-uuid": model}


@pytest.mark.asyncio
async def test_changed_api_base_is_patched():
    """A model server that moved must be repointed, not left stale."""
    reconciler = _reconciler_with_costs(None)
    model = _make_model("moved-uuid")
    stale = _entry_matching(model, "id-2")
    stale["litellm_params"]["api_base"] = "http://old-ip:8080/v1"

    plan = await reconciler.compute_plan(
        {"moved-uuid": model}, {"moved-uuid": [stale]}
    )

    assert len(plan.patches) == 1
    patch_id, patch_params, _ = plan.patches[0]
    assert patch_id == "id-2"
    assert patch_params["api_base"] == model.api_base


@pytest.mark.asyncio
async def test_changed_mode_is_patched():
    """model_info.mode is the other half of the write, so it is compared too."""
    reconciler = _reconciler_with_costs(None)
    model = _make_model("tts-uuid", mode="text_to_speech", provider="omnivoice", model_id="omnivoice")
    stale = _entry_matching(model, "id-3")
    stale["model_info"]["mode"] = "chat"

    plan = await reconciler.compute_plan(
        {"tts-uuid": model}, {"tts-uuid": [stale]}
    )

    assert len(plan.patches) == 1
    _, _, patch_model_info = plan.patches[0]
    assert patch_model_info["mode"] == "audio_speech"


@pytest.mark.asyncio
async def test_changed_pricing_is_patched():
    """A pricing refresh is a real change and must not be swallowed by the guard."""
    costs = {"input_cost_per_token": 0.000002, "output_cost_per_token": 0.000004}
    reconciler = _reconciler_with_costs(costs)
    model = _make_model("priced-uuid")
    stale = _entry_matching(model, "id-4", costs={"input_cost_per_token": 0.000001})

    plan = await reconciler.compute_plan(
        {"priced-uuid": model}, {"priced-uuid": [stale]}
    )

    assert len(plan.patches) == 1
    _, patch_params, _ = plan.patches[0]
    assert patch_params["input_cost_per_token"] == 0.000002
    assert patch_params["output_cost_per_token"] == 0.000004


@pytest.mark.asyncio
async def test_matching_pricing_is_not_patched():
    """Pricing that already matches must not produce a write."""
    costs = {"input_cost_per_token": 0.000002, "output_cost_per_token": 0.000004}
    reconciler = _reconciler_with_costs(costs)
    model = _make_model("priced-uuid-2")
    entry = _entry_matching(model, "id-5", costs=costs)

    plan = await reconciler.compute_plan(
        {"priced-uuid-2": model}, {"priced-uuid-2": [entry]}
    )

    assert plan.patches == []


@pytest.mark.asyncio
async def test_guard_only_compares_keys_it_would_write():
    """Extra stored keys must not make a patch look necessary.

    LiteLLM merges the patch into the stored params, so keys the controller
    never writes (drop_params, api_key, and the rest) are not its concern.
    """
    reconciler = _reconciler_with_costs(None)
    model = _make_model("extra-uuid")
    entry = _entry_matching(model, "id-6")
    entry["litellm_params"]["drop_params"] = True
    entry["litellm_params"]["api_key"] = ""
    entry["model_info"]["oicm_uuid"] = "abc"
    entry["model_info"]["supported_openai_params"] = ["temperature"]

    plan = await reconciler.compute_plan(
        {"extra-uuid": model}, {"extra-uuid": [entry]}
    )

    assert plan.patches == []


@pytest.mark.asyncio
async def test_missing_stored_params_is_patched():
    """An entry with no litellm_params cannot be assumed correct."""
    reconciler = _reconciler_with_costs(None)
    model = _make_model("bare-uuid")
    entry = {"model_id": "id-7", "model_name": model.model_name, "model_info": {"id": "id-7"}}

    plan = await reconciler.compute_plan(
        {"bare-uuid": model}, {"bare-uuid": [entry]}
    )

    assert len(plan.patches) == 1
    assert plan.patches[0][0] == "id-7"
