"""
SessionIdentityResolver behavior tests: precedence, generated-marker removal,
fork rejection, continuation recovery, and the end-to-end handoff to
DeploymentAffinityCheck (inferred id -> pin -> hit).
"""

import pytest

from litellm.constants import SESSION_ID_GENERATED_METADATA_KEY
from litellm.router_utils.session_identity.config import SessionIdentityConfig
from litellm.router_utils.session_identity.resolver import SessionIdentityResolver

MODEL = "moonshotai/Kimi-K3"


def _config(**overrides) -> SessionIdentityConfig:
    defaults = dict(enabled=True, chunk_size_bytes=512, ttl_seconds=3600, cache_salt="")
    defaults.update(overrides)
    return SessionIdentityConfig(**defaults)


def _big_messages():
    return [
        {"role": "system", "content": "system prompt " * 120},
        {"role": "user", "content": "user question " * 120},
    ]


class _Key:
    api_key = "sk-test"


def _resolver(dual_cache, **cfg) -> SessionIdentityResolver:
    return SessionIdentityResolver(config=_config(**cfg), cache=dual_cache)


async def _hook(resolver, data, key=None):
    return await resolver.async_pre_call_hook(
        user_api_key_dict=key or _Key(), cache=resolver._store.cache, data=data, call_type="acompletion"
    )


@pytest.mark.asyncio
async def test_explicit_metadata_session_id_untouched(dual_cache):
    resolver = _resolver(dual_cache)
    data = {"model": MODEL, "messages": _big_messages(), "metadata": {"session_id": "client-owned"}}
    out = await _hook(resolver, data)
    assert out["metadata"]["session_id"] == "client-owned"
    assert "litellm_session_id_inferred" not in out["metadata"]


@pytest.mark.asyncio
async def test_generated_marker_removed_and_replaced(dual_cache):
    """A policy-generated id is ignored by DeploymentAffinityCheck via the
    marker, so the resolver must REMOVE the marker, preserve the original id
    under a side key, and stamp an inferred id that can actually pin."""
    resolver = _resolver(dual_cache)
    data = {
        "model": MODEL,
        "messages": _big_messages(),
        "metadata": {"session_id": "generated-uuid", SESSION_ID_GENERATED_METADATA_KEY: True},
    }
    out = await _hook(resolver, data)
    md = out["metadata"]
    assert md["litellm_session_id_inferred"] is True
    assert md["litellm_session_id_policy_generated"] == "generated-uuid"
    assert md["session_id"] != "generated-uuid" and md["session_id"]
    assert SESSION_ID_GENERATED_METADATA_KEY not in md  # marker removed


@pytest.mark.asyncio
async def test_header_session_id_skips(dual_cache):
    resolver = _resolver(dual_cache)
    data = {"model": MODEL, "messages": _big_messages(), "litellm_session_id": "from-header", "metadata": {}}
    out = await _hook(resolver, data)
    assert "litellm_session_id_inferred" not in out["metadata"]


@pytest.mark.asyncio
async def test_unsupported_call_type_skips(dual_cache):
    resolver = _resolver(dual_cache)
    data = {"model": MODEL, "input": "embed me", "metadata": {}}
    out = await resolver.async_pre_call_hook(
        user_api_key_dict=_Key(), cache=dual_cache, data=data, call_type="embedding"
    )
    assert "litellm_session_id_inferred" not in out.get("metadata", {})


@pytest.mark.asyncio
async def test_disabled_config_noop(dual_cache):
    resolver = _resolver(dual_cache, enabled=False)
    data = {"model": MODEL, "messages": _big_messages(), "metadata": {}}
    out = await _hook(resolver, data)
    assert "litellm_session_id_inferred" not in out["metadata"]


@pytest.mark.asyncio
async def test_declared_prompt_cache_key_same_id_different_history(dual_cache):
    """Reviewer case #2: same prompt_cache_key, completely different history,
    must resolve to the SAME affinity id (caller explicitly named the session)."""
    resolver = _resolver(dual_cache)
    a = {"model": MODEL, "messages": _big_messages(), "prompt_cache_key": "chat-123", "metadata": {}}
    b = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "totally unrelated " * 200}],
        "prompt_cache_key": "chat-123",
        "metadata": {},
    }
    out_a = await _hook(resolver, a)
    out_b = await _hook(resolver, b)
    assert out_a["metadata"]["session_id"] == out_b["metadata"]["session_id"]
    assert out_a["metadata"]["_session_identity_source"] == "declared"


@pytest.mark.asyncio
async def test_declared_beats_history(dual_cache):
    resolver = _resolver(dual_cache)
    data = {"model": MODEL, "messages": _big_messages(), "prompt_cache_key": "conv-77", "metadata": {}}
    out = await _hook(resolver, data)
    assert out["metadata"]["_session_identity_source"] == "declared"
    assert out["metadata"]["litellm_session_identity_declared"] == "prompt_cache_key\x00conv-77"


@pytest.mark.asyncio
async def test_different_callers_isolated(dual_cache):
    class OtherKey:
        api_key = "sk-other"

    resolver = _resolver(dual_cache)
    out_a = await _hook(resolver, {"model": MODEL, "messages": _big_messages(), "metadata": {}})
    out_b = await _hook(resolver, {"model": MODEL, "messages": _big_messages(), "metadata": {}}, key=OtherKey())
    assert out_a["metadata"]["session_id"] != out_b["metadata"]["session_id"]


@pytest.mark.asyncio
async def test_shared_prefix_fork_gets_fresh_id(dual_cache):
    """Reviewer case #5: chat A and B share a 30KB system prompt but differ in
    their first user turn, neither with an explicit id. B must never inherit A's
    session id."""
    resolver = _resolver(dual_cache)
    shared = "identical fleet prompt " * 200  # ~30KB
    conv_a = {"model": MODEL, "messages": [{"role": "system", "content": shared}, {"role": "user", "content": "question A " * 100}], "metadata": {}}
    conv_b = {"model": MODEL, "messages": [{"role": "system", "content": shared}, {"role": "user", "content": "question B " * 100}], "metadata": {}}

    # A's pre-call hook teaches its lineage synchronously
    out_a = await _hook(resolver, conv_a)
    sid_a = out_a["metadata"]["session_id"]

    # B shares the prefix but is a different conversation: must not reuse A's id
    out_b = await _hook(resolver, conv_b)
    sid_b = out_b["metadata"]["session_id"]
    assert sid_b != sid_a


@pytest.mark.asyncio
async def test_continuation_recovers_id(dual_cache):
    """Reviewer case #6: a genuine turn 2 (append-only growth) recovers the
    stored session id via the chain_len - 1 continuation rule."""
    resolver = _resolver(dual_cache)
    out1 = await _hook(resolver, {"model": MODEL, "messages": _big_messages(), "metadata": {}})
    sid = out1["metadata"]["session_id"]

    grown = _big_messages() + [{"role": "assistant", "content": "answer " * 120}, {"role": "user", "content": "follow up " * 120}]
    out2 = await _hook(resolver, {"model": MODEL, "messages": grown, "metadata": {}})
    assert out2["metadata"]["session_id"] == sid
    assert out2["metadata"]["_session_identity_source"] == "history"


@pytest.mark.asyncio
async def test_compaction_gets_new_id(dual_cache):
    """Reviewer case #7: context compaction (history replaced by a summary) must
    mint a new id, not reuse the old one."""
    resolver = _resolver(dual_cache)
    out1 = await _hook(resolver, {"model": MODEL, "messages": _big_messages(), "metadata": {}})
    sid1 = out1["metadata"]["session_id"]

    compacted = [{"role": "system", "content": "Summary of prior conversation. " * 50}, {"role": "user", "content": "next"}]
    out2 = await _hook(resolver, {"model": MODEL, "messages": compacted, "metadata": {}})
    assert out2["metadata"]["session_id"] != sid1


@pytest.mark.asyncio
async def test_inferred_id_creates_affinity_pin_end_to_end(dual_cache):
    """Reviewer case #1: an inferred id (inferred marker set, GENERATED marker
    absent) must drive DeploymentAffinityCheck to create then hit a session pin."""
    from litellm.router_utils.pre_call_checks.deployment_affinity_check import DeploymentAffinityCheck

    resolver = _resolver(dual_cache)
    out = await _hook(resolver, {"model": MODEL, "messages": _big_messages(), "metadata": {}})
    metadata = out["metadata"]
    assert metadata.get("litellm_session_id_inferred") is True
    assert not metadata.get(SESSION_ID_GENERATED_METADATA_KEY)

    dep = DeploymentAffinityCheck(
        cache=dual_cache, ttl_seconds=3600,
        enable_user_key_affinity=False, enable_responses_api_affinity=False, enable_session_id_affinity=True,
    )
    deployments = [
        {"model_info": {"id": "dep-aaa"}, "model_name": MODEL, "litellm_params": {"model": MODEL}},
        {"model_info": {"id": "dep-bbb"}, "model_name": MODEL, "litellm_params": {"model": MODEL}},
    ]
    request_kwargs = {"litellm_metadata": metadata}
    passed = await dep.async_filter_deployments(model=MODEL, healthy_deployments=deployments, messages=None, request_kwargs=request_kwargs)
    assert len(passed) == 2

    pin_kwargs = dict(request_kwargs)
    pin_kwargs["model_info"] = {"id": "dep-bbb"}
    pin_kwargs["litellm_metadata"] = dict(metadata)
    pin_kwargs["litellm_metadata"]["deployment_model_name"] = MODEL
    await dep.async_pre_call_deployment_hook(kwargs=pin_kwargs, call_type=None)

    narrowed = await dep.async_filter_deployments(model=MODEL, healthy_deployments=deployments, messages=None, request_kwargs=request_kwargs)
    assert [d["model_info"]["id"] for d in narrowed] == ["dep-bbb"]


@pytest.mark.asyncio
async def test_explicit_id_shadow_taught_and_recovered(dual_cache):
    """A client that sends an explicit session id gets its history taught under
    that id; a later request that drops the id recovers the SAME id via history."""
    resolver = _resolver(dual_cache)
    out1 = await _hook(resolver, {"model": MODEL, "messages": _big_messages(), "metadata": {"session_id": "ABC"}})
    assert out1["metadata"]["session_id"] == "ABC"  # explicit id passes through

    # turn 2 drops the explicit id but grows history: recovers ABC, not a new id
    grown = _big_messages() + [{"role": "assistant", "content": "answer " * 120}, {"role": "user", "content": "next " * 120}]
    out2 = await _hook(resolver, {"model": MODEL, "messages": grown, "metadata": {}})
    assert out2["metadata"]["session_id"] == "ABC"


@pytest.mark.asyncio
async def test_declared_id_shadow_taught_and_recovered(dual_cache):
    """A declared prompt_cache_key gets its history taught; a later request
    without the declared field recovers the SAME deterministic id via history."""
    resolver = _resolver(dual_cache)
    out1 = await _hook(resolver, {"model": MODEL, "messages": _big_messages(), "prompt_cache_key": "chat-9", "metadata": {}})
    sid = out1["metadata"]["session_id"]
    assert out1["metadata"]["_session_identity_source"] == "declared"

    grown = _big_messages() + [{"role": "assistant", "content": "answer " * 120}, {"role": "user", "content": "next " * 120}]
    out2 = await _hook(resolver, {"model": MODEL, "messages": grown, "metadata": {}})  # declared field dropped
    assert out2["metadata"]["session_id"] == sid


@pytest.mark.asyncio
async def test_synthesized_teach_failure_fails_open(dual_cache, monkeypatch):
    """A fresh synthesized id is only recoverable if its lineage persists. If the
    teach write fails, the resolver must NOT pin an unrecoverable id."""
    resolver = _resolver(dual_cache)

    async def _failing_teach(*args, **kwargs):
        return False

    monkeypatch.setattr(resolver._store, "teach", _failing_teach)
    out = await _hook(resolver, {"model": MODEL, "messages": _big_messages(), "metadata": {}})
    assert "litellm_session_id_inferred" not in out["metadata"]  # no inferred id stamped
