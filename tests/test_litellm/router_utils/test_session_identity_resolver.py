"""
SessionIdentityResolver behavior tests: skip rules, stamping, and the
end-to-end handoff to DeploymentAffinityCheck (inferred id -> pin -> hit).
"""

import pytest

from litellm.caching.dual_cache import DualCache
from litellm.constants import SESSION_ID_GENERATED_METADATA_KEY, SESSION_IDENTITY_CACHE_KEY_PREFIX
from litellm.router_utils.session_identity.config import SessionIdentityConfig
from litellm.router_utils.session_identity.resolver import SessionIdentityResolver

MODEL = "moonshotai/Kimi-K3"


def _config(**overrides) -> SessionIdentityConfig:
    defaults = dict(
        enabled=True,
        chunk_size_bytes=512,
        max_chain_hashes=64,
        ttl_seconds=3600,
        common_prefix_threshold=3,
        cache_salt="",
    )
    defaults.update(overrides)
    return SessionIdentityConfig(**defaults)


def _big_messages() -> list[dict]:
    return [
        {"role": "system", "content": "system prompt " * 120},
        {"role": "user", "content": "user question " * 120},
    ]


def _key_dict(user_api_key_dict):
    return user_api_key_dict


class _Key:
    api_key = "sk-test"


def _resolver(dual_cache, **cfg) -> SessionIdentityResolver:
    return SessionIdentityResolver(config=_config(**cfg), cache=dual_cache)


@pytest.mark.asyncio
async def test_explicit_metadata_session_id_untouched(dual_cache):
    resolver = _resolver(dual_cache)
    data = {
        "model": MODEL,
        "messages": _big_messages(),
        "metadata": {"session_id": "client-owned"},
    }
    out = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data=data, call_type="acompletion")
    assert out["metadata"]["session_id"] == "client-owned"
    assert "litellm_session_id_inferred" not in out["metadata"]


@pytest.mark.asyncio
async def test_generated_marker_skips(dual_cache):
    resolver = _resolver(dual_cache)
    data = {
        "model": MODEL,
        "messages": _big_messages(),
        "metadata": {"session_id": "generated-uuid", SESSION_ID_GENERATED_METADATA_KEY: True},
    }
    out = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data=data, call_type="acompletion")
    assert "litellm_session_id_inferred" not in out["metadata"]


@pytest.mark.asyncio
async def test_header_session_id_skips(dual_cache):
    resolver = _resolver(dual_cache)
    data = {
        "model": MODEL,
        "messages": _big_messages(),
        "litellm_session_id": "from-header",
        "metadata": {},
    }
    out = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data=data, call_type="acompletion")
    assert "litellm_session_id_inferred" not in out["metadata"]


@pytest.mark.asyncio
async def test_unsupported_call_type_skips(dual_cache):
    resolver = _resolver(dual_cache)
    data = {"model": MODEL, "input": "embed me", "metadata": {}}
    out = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data=data, call_type="embedding")
    assert "litellm_session_id_inferred" not in out.get("metadata", {})


@pytest.mark.asyncio
async def test_disabled_config_noop(dual_cache):
    resolver = _resolver(dual_cache, enabled=False)
    data = {"model": MODEL, "messages": _big_messages(), "metadata": {}}
    out = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data=data, call_type="acompletion")
    assert "litellm_session_id_inferred" not in out["metadata"]


@pytest.mark.asyncio
async def test_inferred_id_stamped_and_stable(dual_cache):
    resolver = _resolver(dual_cache)
    data = {"model": MODEL, "messages": _big_messages(), "metadata": {}}
    out1 = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data=data, call_type="acompletion")
    sid1 = out1["metadata"]["session_id"]
    assert sid1
    assert out1["metadata"]["litellm_session_id_inferred"] is True
    # same conversation again resolves to the same id
    data2 = {"model": MODEL, "messages": _big_messages(), "metadata": {}}
    out2 = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data=data2, call_type="acompletion")
    assert out2["metadata"]["session_id"] == sid1


@pytest.mark.asyncio
async def test_different_callers_get_isolated_ids(dual_cache):
    resolver = _resolver(dual_cache)
    class OtherKey:
        api_key = "sk-other"

    data = {"model": MODEL, "messages": _big_messages(), "metadata": {}}
    out_a = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data=dict(data, metadata={}), call_type="acompletion")
    out_b = await resolver.async_pre_call_hook(user_api_key_dict=OtherKey(), cache=dual_cache, data=dict(data, metadata={}), call_type="acompletion")
    # synthesized ids are scoped by caller; identical content from two callers
    # must not share one lineage
    assert out_a["metadata"]["session_id"] != out_b["metadata"]["session_id"]


@pytest.mark.asyncio
async def test_declared_prompt_cache_key_used(dual_cache):
    resolver = _resolver(dual_cache)
    data = {"model": MODEL, "messages": _big_messages(), "prompt_cache_key": "conv-77", "metadata": {}}
    out = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data=data, call_type="acompletion")
    sid = out["metadata"]["session_id"]
    assert sid
    assert out["metadata"].get("litellm_session_identity_declared") == "prompt_cache_key\x00conv-77"


@pytest.mark.asyncio
async def test_success_event_teaches_lineage_and_next_request_matches(dual_cache):
    """The full production loop: hook stamps, success teaches, second request
    with grown history resolves to the SAME id (turn-3 pinning prerequisite)."""
    resolver = _resolver(dual_cache)
    kwargs = {
        "model": MODEL,
        "messages": _big_messages(),
        "litellm_params": {
            "metadata": {"session_id": None, "user_api_key_hash": "hash-1"},
        },
    }
    await resolver.async_log_success_event(kwargs, response_obj=None, start_time=None, end_time=None)
    # nothing stamped on turn 1 -> no teaching
    assert kwargs["litellm_params"]["metadata"].get("session_id") is None

    out1 = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data={"model": MODEL, "messages": _big_messages(), "metadata": {}}, call_type="acompletion")
    sid = out1["metadata"]["session_id"]
    kwargs1 = {
        "model": MODEL,
        "messages": _big_messages(),
        "litellm_params": {"metadata": {"session_id": sid, "user_api_key_hash": "sk-test"}},
    }
    await resolver.async_log_success_event(kwargs1, response_obj=None, start_time=None, end_time=None)

    # turn 2: history grows -> resolves to the same id via stored lineage
    grown = _big_messages() + [{"role": "assistant", "content": "answer " * 120}, {"role": "user", "content": "follow up " * 120}]
    out2 = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data={"model": MODEL, "messages": grown, "metadata": {}}, call_type="acompletion")
    assert out2["metadata"]["session_id"] == sid


@pytest.mark.asyncio
async def test_inferred_id_creates_affinity_pin_end_to_end(dual_cache):
    """
    Regression: an inferred id (inferred marker set, GENERATED marker absent)
    must drive DeploymentAffinityCheck to create and then hit a session pin.
    """
    from litellm.router_utils.pre_call_checks.deployment_affinity_check import (
        DeploymentAffinityCheck,
    )

    resolver = _resolver(dual_cache)
    data = {"model": MODEL, "messages": _big_messages(), "metadata": {}}
    out = await resolver.async_pre_call_hook(user_api_key_dict=_Key(), cache=dual_cache, data=data, call_type="acompletion")
    metadata = out["metadata"]
    assert metadata.get("litellm_session_id_inferred") is True
    assert not metadata.get(SESSION_ID_GENERATED_METADATA_KEY)

    dep = DeploymentAffinityCheck(
        cache=dual_cache,
        ttl_seconds=3600,
        enable_user_key_affinity=False,
        enable_responses_api_affinity=False,
        enable_session_id_affinity=True,
    )
    deployments = [
        {"model_info": {"id": "dep-aaa"}, "model_name": MODEL, "litellm_params": {"model": MODEL}},
        {"model_info": {"id": "dep-bbb"}, "model_name": MODEL, "litellm_params": {"model": MODEL}},
    ]
    request_kwargs = {"litellm_metadata": metadata}

    # turn 2 with no pin yet: whole pool passes through
    passed = await dep.async_filter_deployments(
        model=MODEL, healthy_deployments=deployments, messages=None, request_kwargs=request_kwargs
    )
    assert len(passed) == 2

    # the affinity check's own pre-call hook writes the pin after selection
    pin_kwargs = dict(request_kwargs)
    pin_kwargs["model_info"] = {"id": "dep-bbb"}
    pin_kwargs["litellm_metadata"] = dict(metadata)
    pin_kwargs["litellm_metadata"]["deployment_model_name"] = MODEL
    await dep.async_pre_call_deployment_hook(kwargs=pin_kwargs, call_type=None)

    # turn 3: same inferred id -> narrowed to the pinned deployment
    narrowed = await dep.async_filter_deployments(
        model=MODEL, healthy_deployments=deployments, messages=None, request_kwargs=request_kwargs
    )
    assert [d["model_info"]["id"] for d in narrowed] == ["dep-bbb"]
