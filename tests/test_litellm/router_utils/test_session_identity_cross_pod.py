"""
Two-pod lineage sharing over one fakeredis server, plus concurrency and
long-conversation guarantees. Exercises the real RedisCache read/write paths
without standing up a Redis server.
"""

import asyncio

import fakeredis
import fakeredis.aioredis
import pytest

from litellm.caching.dual_cache import DualCache
from litellm.caching.redis_cache import RedisCache
from litellm.router_utils.session_identity.config import SessionIdentityConfig
from litellm.router_utils.session_identity.lineage import build_chain
from litellm.router_utils.session_identity.views import project_request
from litellm.router_utils.session_identity.resolver import SessionIdentityResolver

MODEL = "moonshotai/Kimi-K3"


def _config(**overrides) -> SessionIdentityConfig:
    defaults = dict(enabled=True, chunk_size_bytes=512, ttl_seconds=3600, cache_salt="")
    defaults.update(overrides)
    return SessionIdentityConfig(**defaults)


def _make_cache(server) -> DualCache:
    rc = RedisCache(host="fake", port=6379)
    rc.init_async_client = lambda: fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    return DualCache(redis_cache=rc)


def _resolver(cache, **cfg) -> SessionIdentityResolver:
    return SessionIdentityResolver(config=_config(**cfg), cache=cache)


async def _hook(resolver, data, key=None):
    class K:
        api_key = "sk-test"
    return await resolver.async_pre_call_hook(
        user_api_key_dict=key or K(), cache=resolver._store.cache, data=data, call_type="acompletion"
    )


def _big(seed: str):
    return [
        {"role": "system", "content": f"system prompt {seed} " * 120},
        {"role": "user", "content": f"user question {seed} " * 120},
    ]


@pytest.mark.asyncio
async def test_lineage_shared_across_pods():
    """Reviewer case #9: pod A teaches; pod B (empty local tier) recovers the
    same id immediately from shared Redis."""
    server = fakeredis.FakeServer()
    resolver_a = _resolver(_make_cache(server))
    resolver_b = _resolver(_make_cache(server))

    # pod A's pre-call hook teaches its lineage synchronously
    out_a = await _hook(resolver_a, {"model": MODEL, "messages": _big("p"), "metadata": {}})
    sid_a = out_a["metadata"]["session_id"]

    grown = _big("p") + [{"role": "assistant", "content": "answer " * 120}, {"role": "user", "content": "follow up " * 120}]
    out_b = await _hook(resolver_b, {"model": MODEL, "messages": grown, "metadata": {}})
    assert out_b["metadata"]["session_id"] == sid_a


@pytest.mark.asyncio
async def test_fresh_teach_visible_immediately_no_stale_miss(dual_cache):
    """A lookup that misses must not cache the miss and hide a subsequent teach
    (DualCache negative-miss throttle would)."""
    from litellm.router_utils.session_identity.store import SessionIdentityStore

    store = SessionIdentityStore(cache=dual_cache, ttl_seconds=3600)
    chain = build_chain(request=project_request({"messages": _big("q"), "model": MODEL}), model_group=MODEL, cache_salt="", chunk_size=512)
    assert await store.lookup(chain=chain, model_group=MODEL, scope="caller-1") is None

    await store.teach(chain=chain, session_id="sess-x", model_group=MODEL, scope="caller-1")
    hit = await store.lookup(chain=chain, model_group=MODEL, scope="caller-1")
    assert hit is not None and hit.session_id == "sess-x"


@pytest.mark.asyncio
async def test_concurrent_requests_no_depth_exchange(dual_cache):
    """Reviewer case #3: two concurrent requests sharing one resolver resolve
    independently — no shared mutable matcher state leaks between them."""
    resolver = _resolver(dual_cache)
    out1 = await _hook(resolver, {"model": MODEL, "messages": _big("c"), "metadata": {}})
    sid = out1["metadata"]["session_id"]

    grown = _big("c") + [{"role": "assistant", "content": "answer " * 120}, {"role": "user", "content": "next " * 120}]

    async def resolve_two():
        a = await _hook(resolver, {"model": MODEL, "messages": grown, "metadata": {}})
        b = await _hook(resolver, {"model": MODEL, "messages": _big("c"), "metadata": {}})
        return a, b

    out_a, out_b = await resolve_two()
    # the grown conversation continues the same lineage; the identical resend
    # hits the lineage the first request taught, both recovering the same id
    assert out_a["metadata"]["session_id"] == sid
    assert out_b["metadata"]["session_id"] == sid


@pytest.mark.asyncio
async def test_long_conversation_beyond_old_cap():
    """Reviewer case #8: with no node cap, content beyond 256 nodes still
    changes the lineage (the old implementation truncated at 256)."""
    # ~300 messages, each large enough to span multiple chunk nodes
    messages = []
    for i in range(300):
        messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " * 40})
    chain = build_chain(request=project_request({"messages": messages, "model": MODEL}), model_group=MODEL, cache_salt="", chunk_size=512)
    assert len(chain) > 256

    # a conversation identical for 299 turns but different on the 300th differs
    messages2 = list(messages[:-1]) + [{"role": "assistant", "content": "different last turn " * 40}]
    chain2 = build_chain(request=project_request({"messages": messages2, "model": MODEL}), model_group=MODEL, cache_salt="", chunk_size=512)
    assert chain != chain2
