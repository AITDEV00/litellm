"""Chain, declared-id, and fork/continuation classification tests."""

from litellm.router_utils.session_identity.lineage import (
    LineageMatch,
    build_chain,
    declared_id,
    root_seed,
    scoped_declared_session_id,
    synthesized_session_id,
)
from litellm.router_utils.session_identity.views import project_request

MODEL = "moonshotai/Kimi-K3"


def _chain(messages, **body):
    return build_chain(request=project_request({"messages": messages, **body}), model_group=MODEL, cache_salt="", chunk_size=512)


def _big(seed: str, extra=None):
    msgs = [
        {"role": "system", "content": f"Big shared system prompt {seed} " * 80},
        {"role": "user", "content": f"User question one {seed} " * 80},
        {"role": "assistant", "content": f"Assistant answer one {seed} " * 80},
    ]
    if extra:
        msgs.append({"role": "user", "content": f"{extra} " * 80})
    return msgs


def test_chain_stable_when_turn_appended():
    c1 = _chain(_big("s1"))
    c2 = _chain(_big("s1", extra="follow up"))
    assert c1
    # the grown conversation shares all of the shorter chain's non-terminal
    # nodes as a prefix; only its terminal differs (terminal anchors the request)
    assert c2[: len(c1) - 1] == c1[:-1]
    assert len(c2) > len(c1)


def test_short_turns_distinguish_conversations():
    base = _big("s9")
    a = _chain(base + [{"role": "user", "content": "a"}])
    b = _chain(base + [{"role": "user", "content": "b"}])
    assert a != b


def test_large_payload_role_diverges_before_content():
    """Two large payloads, identical bytes, different role: chains diverge at
    the first emitted node because role is committed at frame start."""
    payload = "x" * 5000
    a = _chain([{"role": "system", "content": payload}])
    b = _chain([{"role": "user", "content": payload}])
    assert a[0] != b[0]


def test_tool_call_id_changes_identity():
    """Two tool results differing only in tool_call_id must not canonicalize
    identically."""
    a = _chain([{"role": "tool", "tool_call_id": "call-A", "content": "42"}])
    b = _chain([{"role": "tool", "tool_call_id": "call-B", "content": "42"}])
    assert a != b


def test_tool_array_reorder_same_lineage():
    """Name-sorted tool schemas: reordering the tools array does not split a
    conversation lineage."""
    tools = [
        {"type": "function", "function": {"name": "get_weather", "parameters": {}}},
        {"type": "function", "function": {"name": "search", "parameters": {}}},
    ]
    msgs = [{"role": "user", "content": "hi"}]
    a = build_chain(request=project_request({"messages": msgs, "tools": tools}), model_group=MODEL, cache_salt="", chunk_size=512)
    b = build_chain(request=project_request({"messages": msgs, "tools": list(reversed(tools))}), model_group=MODEL, cache_salt="", chunk_size=512)
    assert a == b


def test_same_named_tools_tiebreak_by_schema():
    """Two tools with the same type+name but different schemas must not collapse
    onto one lineage: the canonical blob is the deterministic tie-breaker, so
    reordering them must NOT change identity but differing content must."""
    msgs = [{"role": "user", "content": "hi"}]
    tool_a = {"type": "function", "function": {"name": "search", "parameters": {"type": "string"}}}
    tool_b = {"type": "function", "function": {"name": "search", "parameters": {"type": "object"}}}
    forward = build_chain(request=project_request({"messages": msgs, "tools": [tool_a, tool_b]}), model_group=MODEL, cache_salt="", chunk_size=512)
    reverse = build_chain(request=project_request({"messages": msgs, "tools": [tool_b, tool_a]}), model_group=MODEL, cache_salt="", chunk_size=512)
    # reorder must be stable (deterministic), NOT depend on input order
    assert forward == reverse
    # but a same-named pair with different schema differs from one with identical schema
    dup = build_chain(request=project_request({"messages": msgs, "tools": [tool_a, tool_a]}), model_group=MODEL, cache_salt="", chunk_size=512)
    assert forward != dup


def test_declared_id_namespaced():
    assert declared_id(project_request({"prompt_cache_key": "abc"})) == "prompt_cache_key\x00abc"
    assert declared_id(project_request({"conversation": "conv-1"})) == "conversation\x00conv-1"
    assert declared_id(project_request({"prompt_cache_key": ""})) is None
    assert declared_id(project_request({"prompt_cache_key": 123})) is None
    assert declared_id(project_request({})) is None
    assert declared_id(project_request({"prompt_cache_key": "a", "conversation": "b"})) == "prompt_cache_key\x00a"


def test_declared_id_long_value_distinct():
    a = declared_id(project_request({"conversation": "x" * 300 + "a"}))
    b = declared_id(project_request({"conversation": "x" * 300 + "b"}))
    assert a is not None and b is not None and a != b


def test_scoped_declared_session_id_deterministic():
    d = "prompt_cache_key\x00chat-123"
    s1 = scoped_declared_session_id(d, MODEL, "scope-1")
    assert s1 == scoped_declared_session_id(d, MODEL, "scope-1")
    assert s1 != scoped_declared_session_id(d, MODEL, "scope-2")
    assert s1 != scoped_declared_session_id(d, "other-model", "scope-1")


def test_synthesized_session_id_stable_and_scoped():
    chain = _chain(_big("s4"))
    sid = synthesized_session_id(chain, MODEL, "scope-1")
    assert sid == synthesized_session_id(list(chain), MODEL, "scope-1")
    assert sid != synthesized_session_id(chain[:-1], MODEL, "scope-1")
    assert sid != synthesized_session_id(chain, MODEL, "scope-2")


def test_continuation_vs_fork():
    """The core fork rule: match reaching the stored conversation's last content
    node is a continuation; a match that stops short is a fork."""
    # stored conversation taught with 7 nodes; incoming matched the deepest at 6
    cont = LineageMatch(session_id="s", matched_depth=6, taught_chain_len=7)
    assert cont.is_continuation() is True
    # incoming matched only 3 of 7 -> diverged before the stored convo ended
    fork = LineageMatch(session_id="s", matched_depth=3, taught_chain_len=7)
    assert fork.is_continuation() is False
    # exact repeat (matched full depth)
    repeat = LineageMatch(session_id="s", matched_depth=7, taught_chain_len=7)
    assert repeat.is_continuation() is True
