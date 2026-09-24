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


@pytest.mark.asyncio
async def test_redis_write_failure_means_not_persisted():
    """The DualCache swallows backend write errors, so teach() must write to the
    concrete Redis backend directly. When that backend's pipeline raises, a
    synthesized request must receive NO inferred id (fail-open), not a falsely
    claimed-persisted one."""
    server = fakeredis.FakeServer()
    cache = _make_cache(server)
    resolver = _resolver(cache)

    class _Boom:
        def pipeline(self, transaction=False):
            raise ConnectionError("redis down")

        async def execute(self):
            raise ConnectionError("redis down")

    # poison the raw client the store will obtain from the redis backend
    cache.redis_cache.init_async_client = lambda: _Boom()

    out = await _hook(resolver, {"model": MODEL, "messages": _big("boom"), "metadata": {}})
    assert "litellm_session_id_inferred" not in out["metadata"]  # fail-open: no inferred id


@pytest.mark.asyncio
async def test_teach_authoritative_incremental_suffix(dual_cache):
    """A growing conversation under the same id writes only the appended suffix,
    not the whole lineage each turn."""
    from litellm.router_utils.session_identity.store import SessionIdentityStore

    store = SessionIdentityStore(cache=dual_cache, ttl_seconds=3600)
    chain1 = build_chain(request=project_request({"messages": _big("t1"), "model": MODEL}), model_group=MODEL, cache_salt="", chunk_size=512)
    assert await store.teach_authoritative(chain=chain1, session_id="sess-t", model_group=MODEL, scope="s")

    grown = _big("t1") + [{"role": "assistant", "content": "answer " * 120}, {"role": "user", "content": "next " * 120}]
    chain2 = build_chain(request=project_request({"messages": grown, "model": MODEL}), model_group=MODEL, cache_salt="", chunk_size=512)

    captured: list = []
    original_write = store._write

    async def _spy(cache_list):
        captured.append(len(cache_list))
        return await original_write(cache_list)

    store._write = _spy
    assert await store.teach_authoritative(chain=chain2, session_id="sess-t", model_group=MODEL, scope="s")
    assert captured and captured[0] < len(chain2)  # wrote a suffix, not the full chain


@pytest.mark.asyncio
async def test_teach_authoritative_sid_switch_full_teach(dual_cache):
    """A different authoritative id over the same history rewrites the FULL
    lineage under the new id (it supersedes any prior lineage)."""
    from litellm.router_utils.session_identity.store import SessionIdentityStore

    store = SessionIdentityStore(cache=dual_cache, ttl_seconds=3600)
    chain = build_chain(request=project_request({"messages": _big("t2"), "model": MODEL}), model_group=MODEL, cache_salt="", chunk_size=512)
    assert await store.teach_authoritative(chain=chain, session_id="synthetic-old", model_group=MODEL, scope="s")

    captured: list = []
    original_write = store._write

    async def _spy(cache_list):
        captured.append(len(cache_list))
        return await original_write(cache_list)

    store._write = _spy
    assert await store.teach_authoritative(chain=chain, session_id="real-new", model_group=MODEL, scope="s")
    assert captured and captured[0] == len(chain)  # full teach under the new id
