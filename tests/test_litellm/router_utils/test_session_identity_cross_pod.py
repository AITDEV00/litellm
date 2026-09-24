"""
Two-pod lineage sharing: the central cross-pod guarantee of the design.

Pod A teaches a conversation's lineage into shared Redis; pod B receives the
next turn (possibly seconds later) and must recover the SAME inferred session
id immediately. Uses two independent DualCache instances over one fakeredis
server, which exercises the real RedisCache read/write paths without standing
up a Redis server.

This also guards the Redis-authoritative read path: DualCache throttles
repeated fetches of *missing* keys (~10s) so pod B must not be able to hide
pod A's fresh teach behind a stale local miss.
"""

import fakeredis
import fakeredis.aioredis
import pytest
from unittest.mock import patch

from litellm.caching.dual_cache import DualCache
from litellm.caching.redis_cache import RedisCache
from litellm.router_utils.session_identity.config import SessionIdentityConfig
from litellm.router_utils.session_identity.history_matcher import HistoryMatcher
from litellm.router_utils.session_identity.store import SessionIdentityStore

MODEL = "moonshotai/Kimi-K3"


def _config() -> SessionIdentityConfig:
    return SessionIdentityConfig(
        enabled=True,
        chunk_size_bytes=512,
        max_chain_hashes=64,
        ttl_seconds=3600,
        common_prefix_threshold=3,
        cache_salt="",
    )


def _make_cache(server: fakeredis.FakeServer) -> DualCache:
    rc = RedisCache(host="fake", port=6379)
    rc.init_async_client = lambda: fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    return DualCache(redis_cache=rc)


def _big_messages() -> list[dict]:
    return [
        {"role": "system", "content": "system prompt " * 120},
        {"role": "user", "content": "user question " * 120},
    ]


@pytest.mark.asyncio
async def test_lineage_shared_across_pods():
    server = fakeredis.FakeServer()
    cache_a = _make_cache(server)
    cache_b = _make_cache(server)
    matcher_a = HistoryMatcher(store=SessionIdentityStore(cache=cache_a, ttl_seconds=3600), config=_config())
    matcher_b = HistoryMatcher(store=SessionIdentityStore(cache=cache_b, ttl_seconds=3600), config=_config())

    turn1 = {"messages": _big_messages(), "model": MODEL}
    sid_a1 = await matcher_a.infer_session_id(data=turn1, model_group=MODEL, scope="caller-1")
    assert sid_a1 is not None
    await matcher_a.teach(data=turn1, model_group=MODEL, scope="caller-1", session_id=sid_a1)

    # Pod B: a new turn with grown history. Pod B's in-memory tier is empty,
    # so this must hit the shared Redis and recover pod A's session id.
    grown = _big_messages() + [
        {"role": "assistant", "content": "answer " * 120},
        {"role": "user", "content": "follow up " * 120},
    ]
    sid_b2 = await matcher_b.infer_session_id(data={"messages": grown, "model": MODEL}, model_group=MODEL, scope="caller-1")
    assert sid_b2 == sid_a1


@pytest.mark.asyncio
async def test_fresh_teach_visible_immediately_no_stale_miss():
    """Pod B must not hide pod A's just-taught lineage behind a cached miss."""
    server = fakeredis.FakeServer()
    cache_a = _make_cache(server)
    cache_b = _make_cache(server)
    store_a = SessionIdentityStore(cache=cache_a, ttl_seconds=3600)
    store_b = SessionIdentityStore(cache=cache_b, ttl_seconds=3600)
    matcher_a = HistoryMatcher(store=store_a, config=_config())
    matcher_b = HistoryMatcher(store=store_b, config=_config())

    turn1 = {"messages": _big_messages(), "model": MODEL}
    chain1 = matcher_a.build_chain(data=turn1, model_group=MODEL)

    # Pod B looks up the chain BEFORE pod A teaches it: records a miss.
    miss = await store_b.lookup(chain=chain1, model_group=MODEL, scope="caller-1")
    assert miss is None

    # Pod A teaches.
    sid_a1 = await matcher_a.infer_session_id(data=turn1, model_group=MODEL, scope="caller-1")
    await matcher_a.teach(data=turn1, model_group=MODEL, scope="caller-1", session_id=sid_a1)

    # Pod B looks up again immediately. DualCache's negative-miss throttle
    # would let pod B's cached "does not exist" answer hide the fresh teach;
    # the Redis-authoritative path must not.
    hit = await store_b.lookup(chain=chain1, model_group=MODEL, scope="caller-1")
    assert hit is not None
    assert hit[0] == sid_a1
