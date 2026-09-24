"""Hypothesis property-based tests for the lineage chain.

Reviewer case #10: fuzz UTF-8 around chunk boundaries, message boundaries,
tool calls, and random append-only histories, asserting the chain's core
invariants hold for arbitrary inputs.
"""

import pytest
from hypothesis import given, settings, strategies as st

from litellm.router_utils.session_identity.lineage import build_chain

MODEL = "m"

# text that exercises multi-byte UTF-8 (accents, CJK, emoji) and ASCII
_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),  # no lone surrogates (invalid UTF-8)
    min_size=0,
    max_size=400,
)

_message = st.fixed_dictionaries(
    {"role": st.sampled_from(["system", "user", "assistant", "tool"]), "content": _text}
)

_messages = st.lists(_message, min_size=1, max_size=30)

_chunk_size = st.integers(min_value=256, max_value=2048)


@given(messages=_messages, chunk_size=_chunk_size)
@settings(max_examples=80, deadline=None)
def test_chain_deterministic(messages, chunk_size):
    """Same input always produces the same chain."""
    a = build_chain(data={"messages": messages}, model_group=MODEL, cache_salt="", chunk_size=chunk_size)
    b = build_chain(data={"messages": messages}, model_group=MODEL, cache_salt="", chunk_size=chunk_size)
    assert a == b
    assert len(a) > 0


@given(messages=_messages, extra=_message, chunk_size=_chunk_size)
@settings(max_examples=80, deadline=None)
def test_append_only_growth_is_prefix_stable(messages, extra, chunk_size):
    """Appending a message never moves earlier non-terminal nodes: the grown
    chain shares the shorter chain's non-terminal prefix (continuation rule)."""
    short = build_chain(data={"messages": messages}, model_group=MODEL, cache_salt="", chunk_size=chunk_size)
    grown = build_chain(data={"messages": messages + [extra]}, model_group=MODEL, cache_salt="", chunk_size=chunk_size)
    # non-terminal nodes are a strict prefix of the grown chain
    assert grown[: len(short) - 1] == short[:-1]
    assert len(grown) > len(short)


@given(messages=_messages, other=_messages, chunk_size=_chunk_size)
@settings(max_examples=60, deadline=None)
def test_distinct_conversations_diverge(messages, other, chunk_size):
    """Two conversations with different content (not a pure append) diverge."""
    if messages == other:
        return
    a = build_chain(data={"messages": messages}, model_group=MODEL, cache_salt="", chunk_size=chunk_size)
    b = build_chain(data={"messages": other}, model_group=MODEL, cache_salt="", chunk_size=chunk_size)
    # not a strict equality requirement in general (one could be a prefix of
    # the other), but they must not be identical
    assert a != b


@given(content=_text, chunk_size=_chunk_size)
@settings(max_examples=60, deadline=None)
def test_role_changes_identity(content, chunk_size):
    """Same content bytes under different roles produce different chains."""
    a = build_chain(data={"messages": [{"role": "user", "content": content}]}, model_group=MODEL, cache_salt="", chunk_size=chunk_size)
    b = build_chain(data={"messages": [{"role": "system", "content": content}]}, model_group=MODEL, cache_salt="", chunk_size=chunk_size)
    assert a != b


@given(messages=_messages, tool_calls=st.lists(
    st.fixed_dictionaries({"id": st.text(min_size=1, max_size=20), "type": st.just("function"),
                           "function": st.fixed_dictionaries({"name": st.text(min_size=1, max_size=20), "arguments": st.text(max_size=50)})}),
    min_size=1, max_size=3,
))
@settings(max_examples=50, deadline=None)
def test_tool_calls_affect_identity(messages, tool_calls):
    """Adding tool_calls changes the chain (semantic fields are framed)."""
    with_tc = list(messages) + [{"role": "assistant", "content": "", "tool_calls": tool_calls}]
    without_tc = list(messages) + [{"role": "assistant", "content": ""}]
    a = build_chain(data={"messages": with_tc}, model_group=MODEL, cache_salt="", chunk_size=512)
    b = build_chain(data={"messages": without_tc}, model_group=MODEL, cache_salt="", chunk_size=512)
    assert a != b
