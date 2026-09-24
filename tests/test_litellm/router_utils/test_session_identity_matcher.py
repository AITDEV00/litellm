"""Tests for the lineage matcher and common-prefix discriminator."""

import pytest

from litellm.caching.dual_cache import DualCache
from litellm.router_utils.session_identity.config import SessionIdentityConfig
from litellm.router_utils.session_identity.history_matcher import HistoryMatcher
from litellm.router_utils.session_identity.store import SessionIdentityStore

MODEL = "moonshotai/Kimi-K3"
SCOPE = "caller-1"


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


def _big_messages(seed: str, extra_user: str | None = None) -> list[dict]:
    """A conversation large enough to produce several chain hashes."""
    messages = [
        {"role": "system", "content": f"Big shared system prompt {seed} " * 80},
        {"role": "user", "content": f"User question one {seed} " * 80},
        {"role": "assistant", "content": f"Assistant answer one {seed} " * 80},
    ]
    if extra_user:
        messages.append({"role": "user", "content": f"{extra_user} " * 80})
    return messages


@pytest.mark.asyncio
async def test_lookup_miss_then_teach_then_hit(dual_cache):
    store = SessionIdentityStore(cache=dual_cache, ttl_seconds=3600)
    matcher = HistoryMatcher(store=store, config=_config())

    data = {"messages": _big_messages("s1"), "model": MODEL}
    chain = matcher.build_chain(data=data, model_group=MODEL)
    assert len(chain) >= 2

    miss = await store.lookup(chain=chain, model_group=MODEL, scope=SCOPE)
    assert miss is None

    await store.teach(chain=chain, session_id="sess-1", model_group=MODEL, scope=SCOPE)
    hit = await store.lookup(chain=chain, model_group=MODEL, scope=SCOPE)
    assert hit is not None
    session_id, depth = hit
    assert session_id == "sess-1"
    assert depth == len(chain)


@pytest.mark.asyncio
async def test_lookup_finds_deepest_continuation(dual_cache):
    """A grown conversation hits at its full depth; the original prefix still resolves."""
    store = SessionIdentityStore(cache=dual_cache, ttl_seconds=3600)
    matcher = HistoryMatcher(store=store, config=_config())

    turn1 = {"messages": _big_messages("s2"), "model": MODEL}
    turn2 = {"messages": _big_messages("s2", extra_user="follow up"), "model": MODEL}
    chain1 = matcher.build_chain(data=turn1, model_group=MODEL)
    chain2 = matcher.build_chain(data=turn2, model_group=MODEL)
    assert chain2[: len(chain1)] == chain1

    await store.teach(chain=chain2, session_id="sess-2", model_group=MODEL, scope=SCOPE)

    hit1 = await store.lookup(chain=chain1, model_group=MODEL, scope=SCOPE)
    assert hit1 is not None and hit1[0] == "sess-2"
    hit2 = await store.lookup(chain=chain2, model_group=MODEL, scope=SCOPE)
    assert hit2 is not None and hit2[0] == "sess-2" and hit2[1] == len(chain2)


@pytest.mark.asyncio
async def test_scope_isolation(dual_cache):
    """Different caller scopes must not see each other's lineage."""
    store = SessionIdentityStore(cache=dual_cache, ttl_seconds=3600)
    matcher = HistoryMatcher(store=store, config=_config())
    data = {"messages": _big_messages("s3"), "model": MODEL}
    chain = matcher.build_chain(data=data, model_group=MODEL)
    await store.teach(chain=chain, session_id="sess-a", model_group=MODEL, scope="scope-a")
    assert await store.lookup(chain=chain, model_group=MODEL, scope="scope-b") is None


@pytest.mark.asyncio
async def test_infer_session_id_stable_for_same_conversation(dual_cache):
    """Fresh conversations get a synthesized id that is stable across calls."""
    matcher = HistoryMatcher(store=SessionIdentityStore(cache=dual_cache), config=_config())
    data = {"messages": _big_messages("s4"), "model": MODEL}
    sid1 = await matcher.infer_session_id(data=data, model_group=MODEL, scope=SCOPE)
    sid2 = await matcher.infer_session_id(data=data, model_group=MODEL, scope=SCOPE)
    assert sid1 is not None and sid1 == sid2


@pytest.mark.asyncio
async def test_small_request_infers_nothing(dual_cache):
    """A tiny request produces no complete chunk, so no id is inferred."""
    matcher = HistoryMatcher(store=SessionIdentityStore(cache=dual_cache), config=_config())
    data = {"messages": [{"role": "user", "content": "hi"}], "model": MODEL}
    assert await matcher.infer_session_id(data=data, model_group=MODEL, scope=SCOPE) is None


@pytest.mark.asyncio
async def test_common_prefix_excluded_from_matching(dual_cache):
    """
    Two sessions sharing a huge system prompt: the shared leading hashes become
    common (threshold reached) and matching must resolve on the distinguishing
    suffix - or not at all when nothing distinguishes them.
    """
    store = SessionIdentityStore(cache=dual_cache, ttl_seconds=3600)
    matcher = HistoryMatcher(store=store, config=_config(common_prefix_threshold=3))

    shared_system = "identical fleet prompt " * 200
    conv_a = {"messages": [{"role": "system", "content": shared_system}, {"role": "user", "content": "question A " * 100}], "model": MODEL}
    conv_b = {"messages": [{"role": "system", "content": shared_system}, {"role": "user", "content": "question B " * 100}], "model": MODEL}

    chain_a = matcher.build_chain(data=conv_a, model_group=MODEL)
    chain_b = matcher.build_chain(data=conv_b, model_group=MODEL)
    assert chain_a[:2] == chain_b[:2]  # shared prefix produces shared hashes
    assert chain_a != chain_b  # but the conversations differ deeper

    # teach through the matcher (production path): teach() both binds the
    # lineage and updates common-hash counters
    await matcher.teach(data=conv_b, model_group=MODEL, scope="other-1", session_id="other-1")
    await matcher.teach(data=conv_b, model_group=MODEL, scope="other-2", session_id="other-2")
    await matcher.teach(data=conv_a, model_group=MODEL, scope=SCOPE, session_id="sess-a")

    # the shared leading hash must now be common
    tracker = matcher.common_prefixes
    assert await tracker.is_common(chain_a[0], MODEL, SCOPE) is True
    distinguishing_a = await tracker.filter_common(chain=chain_a, model_group=MODEL, scope=SCOPE)
    assert chain_a[0] not in distinguishing_a

    # resolving session B (a different caller, same shared prefix) must not
    # come back as session A's id through the common hashes
    resolved_b = await matcher.resolve(chain=chain_b, model_group=MODEL, scope="caller-2")
    assert resolved_b != "sess-a"


@pytest.mark.asyncio
async def test_teach_marks_shared_hashes_common(dual_cache):
    """Observing the same hashes under two sessions drives them past threshold."""
    store = SessionIdentityStore(cache=dual_cache, ttl_seconds=3600)
    matcher = HistoryMatcher(store=store, config=_config(common_prefix_threshold=2))
    data = {"messages": _big_messages("s5"), "model": MODEL}
    chain = matcher.build_chain(data=data, model_group=MODEL)

    await matcher.teach(data=data, model_group=MODEL, scope="s1", session_id="t1")
    assert await matcher.common_prefixes.is_common(chain[0], MODEL, "s1") is False
    await matcher.teach(data=data, model_group=MODEL, scope="s2", session_id="t2")
    # two distinct sessions have served these hashes -> common at threshold 2
    assert await matcher.common_prefixes.is_common(chain[0], MODEL, "s1") is True
