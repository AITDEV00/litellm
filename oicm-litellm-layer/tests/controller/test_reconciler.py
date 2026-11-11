from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from controller.capabilities import CapabilityFacts
from controller.models import OicmModel, build_model, to_litellm_mode
from controller.pricing.models import PricingResult
from controller.reconciler import SyncPlan, SyncReconciler, _summaries_to_models


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
        "model_info": {
            "id": model_id,
            "mode": mode,
            "oicm_uuid": "abc",
            "oicm_source": "local",
        },
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
async def test_patch_stamps_task_and_health_voice_for_task_declared_tts():
    """A task-declared TTS row converges in one patch carrying both stamps.

    The mode correction alone would leave the health probe sending voice=
    "alloy" (a 400 on Drama 3), and the task stamp alone would leave the
    /endpoints surface inferring intent from mode.
    """
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    reconciler = SyncReconciler(MagicMock(), pricing)

    model = replace(
        _make_model("tts-uuid", mode="text_to_speech", model_id="fishaudio/drama-3"),
        inference_task="Text-to-Speech",
    )
    litellm_by_key = {
        "tts-uuid": [_make_litellm_entry("litellm-id-t", model_name="hosted_vllm/fishaudio/drama-3")]
    }

    plan = await reconciler.compute_plan({"tts-uuid": model}, litellm_by_key)

    assert len(plan.patches) == 1
    _, _, patch_model_info = plan.patches[0]
    assert patch_model_info["oicm_task"] == "Text-to-Speech"
    assert patch_model_info["health_check_voice"] == "default"


@pytest.mark.asyncio
async def test_patch_keeps_path_detected_tts_voice_contract():
    """Path-detected TTS (hamsa, omnivoice) must not gain the probe voice.

    hamsa 400s a speaker it does not know; "default" is a Drama 3-ism, so the
    stamp stays scoped to task-declared TTS.
    """
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    reconciler = SyncReconciler(MagicMock(), pricing)

    model = _make_model("tts-uuid", mode="text_to_speech", provider="omnivoice", model_id="omnivoice")
    litellm_by_key = {
        "tts-uuid": [_make_litellm_entry("litellm-id-o", model_name="omnivoice/omnivoice")]
    }

    plan = await reconciler.compute_plan({"tts-uuid": model}, litellm_by_key)

    _, _, patch_model_info = plan.patches[0]
    assert "health_check_voice" not in patch_model_info
    assert "oicm_task" not in patch_model_info


@pytest.mark.asyncio
async def test_patch_never_stamps_a_voice_the_provider_rejects():
    """The patch lane has the same provider scope as registration.

    A hamsa row whose OICM declaration says Text-to-Speech must not gain
    "default" through a backfill patch any more than through registration.
    """
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    reconciler = SyncReconciler(MagicMock(), pricing)

    model = replace(
        _make_model("tts-uuid", mode="text_to_speech", provider="hamsa", model_id="hamsa-tts-new"),
        inference_task="Text-to-Speech",
    )
    litellm_by_key = {
        "tts-uuid": [_make_litellm_entry("litellm-id-h", model_name="hamsa/hamsa-tts-new")]
    }

    plan = await reconciler.compute_plan({"tts-uuid": model}, litellm_by_key)

    _, _, patch_model_info = plan.patches[0]
    assert patch_model_info["oicm_task"] == "Text-to-Speech"
    assert "health_check_voice" not in patch_model_info


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
async def test_register_uses_the_served_model_id():
    """The registered row must carry the served id and the deployment's api_base."""
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    reconciler = SyncReconciler(MagicMock(), pricing)

    dep_uuid = "doc-uuid"
    k8s_models = {dep_uuid: _make_multi_model(dep_uuid, "PP-DocLayoutV3")}

    plan = await reconciler.compute_plan(k8s_models, {})

    assert len(plan.registers) == 1
    registered_model, _ = plan.registers[0]
    assert registered_model.model_id == "PP-DocLayoutV3"
    assert registered_model.api_base == "http://s-doc-uuid.adeo.svc.cluster.local:8080/v1"


@pytest.mark.asyncio
async def test_register_matches_on_uuid_even_when_oicm_label_differs():
    """The join must survive OICM's GUI label differing from the served id.

    OICM's `model_name` is the deployment name shown in its GUI, not the id the
    server serves. Keying the join on the uuid means the gateway row still
    matches even when those two strings have nothing in common.
    """
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    reconciler = SyncReconciler(MagicMock(), pricing)

    dep_uuid = "renamed-uuid"
    model = _make_multi_model(dep_uuid, "Qwen/Qwen3.6-35B-A3B-FP8")
    k8s_models = {dep_uuid: model}
    # The gateway row is keyed by uuid and holds the served id the controller
    # registered, which is what discovery also reports. OICM's label, which the
    # reconciler never sees, could be anything.
    entry = _make_litellm_entry(
        "id-1", model_name="Qwen/Qwen3.6-35B-A3B-FP8"
    )
    entry["litellm_params"] = {
        "model": "hosted_vllm/Qwen/Qwen3.6-35B-A3B-FP8",
        "api_base": model.api_base,
    }
    entry["model_info"]["mode"] = "chat"

    plan = await reconciler.compute_plan(k8s_models, {dep_uuid: [entry]})

    assert plan.registers == []
    assert plan.patches == []
    assert plan.deletes == []


@pytest.mark.asyncio
async def test_execute_keys_state_by_uuid():
    """execute() must key state and the id map by the deployment uuid."""
    pricing = MagicMock()
    pricing.resolve = AsyncMock(return_value=None)
    litellm = MagicMock()
    # batch() returns (deleted, [registered ids in order], patched)
    litellm.batch = AsyncMock(return_value=(0, ["id-a", "id-b"], 0))
    reconciler = SyncReconciler(litellm, pricing)

    model_a = _make_multi_model("uuid-a", "m-a")
    model_b = _make_multi_model("uuid-b", "m-b")
    plan = SyncPlan()
    plan.registers = [(model_a, None), (model_b, None)]

    deleted, registered, patched = await reconciler.execute(plan)

    assert (deleted, registered, patched) == (0, 2, 0)
    assert set(plan.new_state.keys()) == {"uuid-a", "uuid-b"}
    assert plan.new_id_map == {"uuid-a": "id-a", "uuid-b": "id-b"}


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

    models = [
        _make_multi_model("uuid-a", "m-a"),
        _make_multi_model("uuid-b", "m-b"),
        _make_multi_model("uuid-c", "m-c"),
    ]
    plan = SyncPlan()
    plan.registers = [(m, None) for m in models]

    deleted, registered, patched = await reconciler.execute(plan)

    assert (deleted, registered, patched) == (0, 3, 0)
    assert plan.new_id_map == {"uuid-a": "id-a", "uuid-c": "id-c"}
    # The failed register (uuid-b) must NOT be in state (no id assigned), and its
    # neighbors must keep their correct ids (no shifting).
    assert "uuid-b" not in plan.new_state
    assert plan.new_state["uuid-a"].model_id == "m-a"
    assert plan.new_state["uuid-c"].model_id == "m-c"


def _entry_matching(model, model_id, costs=None):
    """A gateway entry already holding exactly what compute_plan would patch."""
    params = {"model": f"{model.provider}/{model.model_id}", "api_base": model.api_base}
    if costs:
        params.update(costs)
    info = {
        "id": model_id,
        "mode": to_litellm_mode(model.mode),
        "oicm_uuid": model.uuid,
        "oicm_source": "local",
    }
    info.update(model.token_budgets)
    info.update(model.capability_stamp)
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


@pytest.mark.asyncio
async def test_rows_without_token_budgets_are_backfilled():
    """Every existing chat row predates the budget stamp, so the first cycle
    after rollout must patch the split in (one write per model, one time).

    The stored listing carries discovery-merged window values in the token
    fields, so the guard comparing desired-vs-stored must see a difference
    and plan the patch.
    """
    reconciler = _reconciler_with_costs(None)
    model = replace(_make_model("legacy-uuid"), max_model_len=524288)
    entry = _entry_matching(model, "id-9")
    # A pre-rollout row: stored token fields hold the discovery-merged window.
    entry["model_info"].update(
        {
            "max_tokens": 524288,
            "max_input_tokens": 524288,
            "max_output_tokens": 524288,
        }
    )

    plan = await reconciler.compute_plan(
        {"legacy-uuid": model}, {"legacy-uuid": [entry]}
    )

    assert len(plan.patches) == 1
    _, _, patch_model_info = plan.patches[0]
    assert patch_model_info["max_output_tokens"] == 64000
    assert patch_model_info["max_input_tokens"] == 524288 - 64000 - 64
    assert patch_model_info["context_length"] == 524288
    assert patch_model_info["max_tokens"] == 64000


@pytest.mark.asyncio
async def test_rows_with_matching_budgets_are_not_repatched():
    """Steady state after the backfill: a row already carrying the split must
    not be rewritten (every write reloads every model on every replica)."""
    reconciler = _reconciler_with_costs(None)
    model = replace(_make_model("stable-budget-uuid"), max_model_len=524288)
    entry = _entry_matching(model, "id-10")

    plan = await reconciler.compute_plan(
        {"stable-budget-uuid": model}, {"stable-budget-uuid": [entry]}
    )

    assert plan.patches == []


@pytest.mark.asyncio
async def test_unprobed_model_never_receives_a_budget_patch():
    """A chat model with no advertised window must not be churned: the patch
    guard compares only the keys the controller would write, and with no
    window there are no token keys to compare."""
    reconciler = _reconciler_with_costs(None)
    model = _make_model("unprobed-uuid")  # max_model_len defaults to None
    entry = _entry_matching(model, "id-11")
    entry["model_info"].update(
        {"max_tokens": 131072, "max_input_tokens": 131072, "max_output_tokens": 131072}
    )

    plan = await reconciler.compute_plan(
        {"unprobed-uuid": model}, {"unprobed-uuid": [entry]}
    )

    assert plan.patches == []


@pytest.mark.asyncio
async def test_rows_without_capability_stamps_are_backfilled():
    """Every existing chat row predates the capability stamp, so the first
    cycle after rollout must patch the flags in (one write per model, one
    time, riding the same patch as the token backfill)."""
    reconciler = _reconciler_with_costs(None)
    model = replace(
        _make_model("legacy-caps-uuid"),
        capability_facts=CapabilityFacts(accepts_image=True, accepts_audio=False),
    )
    entry = _entry_matching(model, "id-12")
    # A pre-rollout row carries no capability keys at all.
    entry["model_info"].pop("supports_vision", None)
    entry["model_info"].pop("supports_audio_input", None)
    entry["model_info"].pop("architecture", None)

    plan = await reconciler.compute_plan(
        {"legacy-caps-uuid": model}, {"legacy-caps-uuid": [entry]}
    )

    assert len(plan.patches) == 1
    _, _, patch_model_info = plan.patches[0]
    assert patch_model_info["supports_vision"] is True
    assert patch_model_info["supports_audio_input"] is False


@pytest.mark.asyncio
async def test_rows_with_matching_capability_stamps_are_not_repatched():
    """Steady state after the backfill: a row already carrying the stamp
    must not be rewritten (every write reloads every model on every replica)."""
    reconciler = _reconciler_with_costs(None)
    model = replace(
        _make_model("stable-caps-uuid"),
        capability_facts=CapabilityFacts(accepts_image=False, accepts_audio=False),
    )
    entry = _entry_matching(model, "id-13")

    plan = await reconciler.compute_plan(
        {"stable-caps-uuid": model}, {"stable-caps-uuid": [entry]}
    )

    assert plan.patches == []


@pytest.mark.asyncio
async def test_unprobed_model_never_receives_a_capability_patch():
    """A chat model whose probe returned nothing must not be churned: with
    no verdict there are no capability keys to write, and the guard must not
    interpret stored values as a difference."""
    reconciler = _reconciler_with_costs(None)
    model = _make_model("unprobed-caps-uuid")  # capability_facts defaults to None
    entry = _entry_matching(model, "id-14")
    # Even a row someone patched by hand (e.g. during a round-trip test) holds
    # values the controller would not write; it must not be churned.
    entry["model_info"]["supports_vision"] = True

    plan = await reconciler.compute_plan(
        {"unprobed-caps-uuid": model}, {"unprobed-caps-uuid": [entry]}
    )

    assert plan.patches == []


def _entry_blocked(model, model_id, blocked):
    entry = _entry_matching(model, model_id)
    entry["model_info"]["blocked"] = blocked
    return entry


def _stopped_model(uuid, model_id="test-model"):
    """A deployment OICM still reports but that is no longer serving."""
    return replace(_make_model(uuid, model_id=model_id), serving=False, ready_replicas=0)


@pytest.mark.asyncio
async def test_stopped_deployment_stays_registered_and_is_blocked():
    """A Stopped deployment must stay visible but leave the routing pool.

    OICM keeps the record after the k8s Deployment is deleted, so the model must
    not be removed. Leaving it routable would send every request at a Service
    that no longer exists, so it is paused instead.
    """
    reconciler = _reconciler_with_costs(None)
    model = _stopped_model("stopped-uuid")
    entry = _entry_blocked(_make_model("stopped-uuid"), "id-8", blocked=False)

    plan = await reconciler.compute_plan(
        {"stopped-uuid": model}, {"stopped-uuid": [entry]}
    )

    assert plan.deletes == []
    assert plan.registers == []
    assert plan.blocks == [("id-8", True)]
    assert plan.new_state["stopped-uuid"].serving is False


@pytest.mark.asyncio
async def test_stopped_deployment_does_not_rewrite_config():
    """Pausing must not also rewrite the config, which would reload the gateway."""
    reconciler = _reconciler_with_costs(None)
    model = _stopped_model("stopped-uuid-2")
    entry = _entry_blocked(_make_model("stopped-uuid-2"), "id-9", blocked=True)

    plan = await reconciler.compute_plan(
        {"stopped-uuid-2": model}, {"stopped-uuid-2": [entry]}
    )

    assert plan.patches == []
    assert plan.blocks == []


@pytest.mark.asyncio
async def test_restarted_deployment_is_unblocked():
    """A deployment that came back must become routable again."""
    reconciler = _reconciler_with_costs(None)
    model = _make_model("restarted-uuid")
    entry = _entry_blocked(model, "id-10", blocked=True)

    plan = await reconciler.compute_plan(
        {"restarted-uuid": model}, {"restarted-uuid": [entry]}
    )

    assert plan.blocks == [("id-10", False)]
    assert plan.new_state["restarted-uuid"].serving is True


@pytest.mark.asyncio
async def test_serving_deployment_is_not_blocked():
    """A healthy model must not be paused."""
    reconciler = _reconciler_with_costs(None)
    model = _make_model("healthy-uuid")
    entry = _entry_matching(model, "id-11")

    plan = await reconciler.compute_plan(
        {"healthy-uuid": model}, {"healthy-uuid": [entry]}
    )

    assert plan.blocks == []


@pytest.mark.asyncio
async def test_non_serving_deployment_is_registered_then_blocked():
    """A deployment discovered for the first time while not serving.

    It has to be registered so it is visible, and paused once it has an id,
    because a new model always starts unblocked.
    """
    reconciler = _reconciler_with_costs(None)
    litellm = MagicMock()
    litellm.batch = AsyncMock(return_value=(0, ["id-12"], 0))
    litellm.set_blocked = AsyncMock(return_value=True)
    reconciler.litellm = litellm
    model = _stopped_model("new-stopped-uuid")

    plan = await reconciler.compute_plan({"new-stopped-uuid": model}, {})
    assert len(plan.registers) == 1

    await reconciler.execute(plan)

    litellm.set_blocked.assert_awaited_once_with("id-12", True)
    assert plan.new_id_map[model.deployment_id] == "id-12"


@pytest.mark.asyncio
async def test_alias_expanded_entries_do_not_delete_their_deployment():
    reconciler = _reconciler_with_costs(None)
    canonical = _entry_matching(_make_model("serving-uuid"), "deployment-id")
    alias = {**canonical, "model_name": "short-name"}

    plan = await reconciler.compute_plan(
        {"serving-uuid": _make_model("serving-uuid")},
        {"serving-uuid": [canonical, alias]},
    )

    assert plan.deletes == []
    assert plan.registers == []
    assert plan.new_id_map["serving-uuid"] == "deployment-id"


@pytest.mark.asyncio
async def test_execute_applies_planned_blocks():
    """execute() must issue the block calls compute_plan asked for."""
    reconciler = _reconciler_with_costs(None)
    litellm = MagicMock()
    litellm.batch = AsyncMock(return_value=(0, [], 0))
    litellm.set_blocked = AsyncMock(return_value=True)
    reconciler.litellm = litellm

    plan = SyncPlan()
    plan.blocks = [("id-a", True), ("id-b", False)]

    await reconciler.execute(plan)

    assert litellm.set_blocked.await_count == 2
    litellm.set_blocked.assert_any_await("id-a", True)
    litellm.set_blocked.assert_any_await("id-b", False)


@pytest.mark.asyncio
async def test_deployment_absent_from_oicm_is_deleted():
    """Deletion is signalled by absence from OICM, not by a status value.

    This is the case that must still remove a model: nothing in OICM knows about
    it, so the k8s watch no longer sees it either.
    """
    reconciler = _reconciler_with_costs(None)
    entry = _entry_blocked(_make_model("gone-uuid"), "id-13", blocked=True)

    plan = await reconciler.compute_plan({}, {"gone-uuid": [entry]})

    assert plan.deletes == ["id-13"]
    assert plan.blocks == []
    assert plan.new_state == {}


@pytest.mark.asyncio
async def test_entry_without_oicm_uuid_absent_from_oicm_is_left_alone():
    """A row the controller did not create must never be deleted.

    The gateway also carries admin-added models and the heartbeat rows, none of
    which have an `oicm_uuid`. Absence from OICM is a delete signal only for a
    row that carries one.
    """
    reconciler = _reconciler_with_costs(None)
    entry = _entry_blocked(_make_model("admin-uuid"), "id-admin", blocked=False)
    entry["model_info"].pop("oicm_uuid")

    plan = await reconciler.compute_plan({}, {"admin-uuid": [entry]})

    assert plan.deletes == []


@pytest.mark.asyncio
async def test_cross_cluster_entry_absent_from_oicm_is_deleted():
    """A controller-managed import is removed once OICM stops listing it.

    Scope is the presence of an `oicm_uuid`, not `oicm_source == "local"`:
    once a second cluster exists, a cross-cluster import is just as much the
    controller's to remove as a local row.
    """
    reconciler = _reconciler_with_costs(None)
    entry = _entry_blocked(_make_model("import-uuid"), "id-import", blocked=False)
    entry["model_info"]["oicm_source"] = "submariner:abudhabi"
    entry["model_info"]["oicm_uuid"] = "submariner:abudhabi:import-uuid"

    plan = await reconciler.compute_plan({}, {"import-uuid": [entry]})

    assert plan.deletes == ["id-import"]


def _summary(workload_id, serving, cluster="alain", inference_task=None):
    return SimpleNamespace(
        workspace_id="ws",
        workload_id=workload_id,
        cluster=cluster,
        serving_available=serving,
        inference_task=inference_task,
    )


def test_oicm_placeholder_carries_the_declared_task():
    """A Stopped deployment's task must survive into its placeholder record.

    The placeholder is what the reconciler merges when the k8s side is absent,
    so a row that went Stopped before ever being re-discovered still gains the
    task stamp on its next write.
    """
    models = _summaries_to_models(
        {"stopped-uuid": _summary("stopped-uuid", False, inference_task="Text-to-Speech")}
    )

    assert models["stopped-uuid"].inference_task == "Text-to-Speech"


@pytest.mark.asyncio
async def test_stopped_deployment_known_only_to_oicm_is_paused_not_deleted():
    """A deployment with no k8s object but still listed by OICM must stay.

    This is the whole point of the existence rule: the k8s watch cannot see a
    Stopped deployment, so OICM has to be the source of existence. The row stays
    registered and is paused, which keeps Stopped distinguishable from Deleted.
    """
    reconciler = _reconciler_with_costs(None)
    entry = _entry_blocked(_make_model("stopped-uuid"), "id-14", blocked=False)
    entry["model_name"] = "hosted_vllm/test-model"
    oicm_models = _summaries_to_models({"stopped-uuid": _summary("stopped-uuid", False)})

    plan = await reconciler.compute_plan({}, {"stopped-uuid": [entry]}, oicm_models)

    assert plan.deletes == []
    assert plan.registers == []
    assert plan.blocks == [("id-14", True)]
    # The placeholder is deliberately kept out of new_state: the watch handlers
    # key off it, and a placeholder there would make a later redeploy with the
    # same uuid look like a duplicate ADDED.
    assert "stopped-uuid" not in plan.new_state


@pytest.mark.asyncio
async def test_deployment_absent_from_oicm_is_deleted_when_oicm_is_the_source():
    """Absence from OICM is still a delete once OICM is the existence source."""
    reconciler = _reconciler_with_costs(None)
    entry = _entry_blocked(_make_model("gone-uuid"), "id-15", blocked=True)

    plan = await reconciler.compute_plan({}, {"gone-uuid": [entry]}, {})

    assert plan.deletes == ["id-15"]


@pytest.mark.asyncio
async def test_oicm_only_deployment_is_not_registered():
    """A Stopped deployment OICM knows but the gateway never registered stays out.

    Registering it would create a row with no served model id and an api_base
    pointing at a Service that does not exist, blocked forever. Only the k8s
    watch yields enough to build a real row, so OICM alone is not enough.
    """
    reconciler = _reconciler_with_costs(None)
    oicm_models = _summaries_to_models({"stopped-uuid": _summary("stopped-uuid", False)})

    plan = await reconciler.compute_plan({}, {}, oicm_models)

    assert plan.registers == []
    assert plan.new_state == {}


@pytest.mark.asyncio
async def test_incomplete_poll_never_deletes():
    """A failed source must not make its deployments look deleted.

    When any configured source fails to poll, its deployments are missing from
    `oicm_models` through no fault of their own. Reading that as deletion would
    wipe a whole cluster's rows on a transient OICM error, so nothing is removed
    until a complete cycle says so.
    """
    reconciler = _reconciler_with_costs(None)
    managed = _entry_blocked(_make_model("gone-uuid"), "id-16", blocked=True)

    plan = await reconciler.compute_plan(
        {}, {"gone-uuid": [managed]}, allow_deletes=False
    )

    assert plan.deletes == []


@pytest.mark.asyncio
async def test_complete_poll_deletes_what_oicm_no_longer_lists():
    """The same row is removed once every source has been heard from."""
    reconciler = _reconciler_with_costs(None)
    managed = _entry_blocked(_make_model("gone-uuid"), "id-16", blocked=True)

    plan = await reconciler.compute_plan(
        {}, {"gone-uuid": [managed]}, allow_deletes=True
    )

    assert plan.deletes == ["id-16"]


@pytest.mark.asyncio
async def test_cross_cluster_import_is_not_registered_twice():
    """Regression: a Submariner import must match its own gateway row.

    The import yields `submariner:abudhabi:<uuid>` while the gateway row is keyed
    on the same prefixed value. `deployment_id` strips the prefix, and the
    gateway grouping strips it too, so the two agree. If either side stopped
    stripping, the import would look like a brand-new deployment and be
    registered a second time on every cycle.
    """
    reconciler = _reconciler_with_costs(None)
    imported = build_model(
        uuid="submariner:abudhabi:766b1720",
        model_id="zai-org/GLM-5.2-FP8",
        ready_replicas=1,
        total_replicas=1,
        source="submariner:abudhabi",
        cluster="abudhabi",
    )
    entry = _entry_matching(imported, "id-ad")
    entry["model_info"]["oicm_uuid"] = "submariner:abudhabi:766b1720"
    entry["model_info"]["oicm_source"] = "submariner:abudhabi"

    plan = await reconciler.compute_plan(
        {"766b1720": imported}, {"766b1720": [entry]}
    )

    assert plan.registers == []
    assert plan.deletes == []

