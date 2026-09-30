import json
import logging
from typing import Any, Dict, List, Optional, Set, Union

import pytest


import asyncio
from unittest.mock import MagicMock, patch


from litellm.caching.caching import DualCache
from litellm.caching.redis_cache import RedisCircuitBreakerOpenError, RedisPipelineIncrementOperation
from litellm.router_strategy.base_routing_strategy import BaseRoutingStrategy


@pytest.fixture
async def mock_dual_cache():
    dual_cache = MagicMock(spec=DualCache)
    dual_cache.in_memory_cache = MagicMock()
    dual_cache.redis_cache = MagicMock()

    # Set up async method mocks to return coroutines
    future1: asyncio.Future[None] = asyncio.Future()
    future1.set_result(None)
    dual_cache.in_memory_cache.async_increment.return_value = future1

    future2: asyncio.Future[None] = asyncio.Future()
    future2.set_result(None)
    dual_cache.redis_cache.async_increment_pipeline.return_value = future2

    future3: asyncio.Future[None] = asyncio.Future()
    future3.set_result(None)
    dual_cache.in_memory_cache.async_set_cache.return_value = future3

    # Fix for async_batch_get_cache
    batch_future: asyncio.Future[Dict[str, str]] = asyncio.Future()
    batch_future.set_result({"key1": "10.0", "key2": "20.0"})
    dual_cache.redis_cache.async_batch_get_cache.return_value = batch_future

    return dual_cache


@pytest.fixture
async def base_strategy(mock_dual_cache):
    return BaseRoutingStrategy(
        dual_cache=mock_dual_cache,
        should_batch_redis_writes=False,
        default_sync_interval=1,
    )


@pytest.mark.asyncio
async def test_increment_value_in_current_window(base_strategy, mock_dual_cache):
    # Test incrementing value in current window
    key = "test_key"
    value = 10.0
    ttl = 3600

    await base_strategy._increment_value_in_current_window(key, value, ttl)

    # Verify in-memory cache was incremented
    mock_dual_cache.in_memory_cache.async_increment.assert_called_once_with(
        key=key, value=value, ttl=ttl
    )

    # Verify operation was queued for Redis
    assert len(base_strategy.redis_increment_operation_queue) == 1
    queued_op = base_strategy.redis_increment_operation_queue[0]
    assert isinstance(queued_op, dict)
    assert queued_op["key"] == key
    assert queued_op["increment_value"] == value
    assert queued_op["ttl"] == ttl


@pytest.mark.asyncio
async def test_push_in_memory_increments_to_redis(base_strategy, mock_dual_cache):
    # Add some operations to the queue
    base_strategy.redis_increment_operation_queue = [
        RedisPipelineIncrementOperation(key="key1", increment_value=10, ttl=3600),
        RedisPipelineIncrementOperation(key="key2", increment_value=20, ttl=3600),
    ]

    await base_strategy._push_in_memory_increments_to_redis()

    # Verify Redis pipeline was called
    mock_dual_cache.redis_cache.async_increment_pipeline.assert_called_once()
    # Verify queue was cleared
    assert len(base_strategy.redis_increment_operation_queue) == 0


@pytest.mark.asyncio
async def test_sync_in_memory_spend_with_redis(base_strategy, mock_dual_cache):
    from litellm.types.caching import RedisPipelineIncrementOperation

    # Setup test data
    base_strategy.in_memory_keys_to_update = {"key1"}
    base_strategy.redis_increment_operation_queue = [
        RedisPipelineIncrementOperation(key="key1", increment_value=10, ttl=3600),
    ]

    # Mock the in-memory cache batch get responses for before snapshot
    in_memory_before_future: asyncio.Future[List[str]] = asyncio.Future()
    in_memory_before_future.set_result(["5.0"])  # Initial values
    mock_dual_cache.in_memory_cache.async_batch_get_cache.return_value = (
        in_memory_before_future
    )

    # Mock Redis batch get response
    redis_future: asyncio.Future[Dict[str, str]] = asyncio.Future()
    redis_future.set_result([15.0])  # Redis values
    mock_dual_cache.redis_cache.async_increment_pipeline.return_value = redis_future

    # Mock in-memory get for after snapshot
    in_memory_after_future: asyncio.Future[Optional[str]] = asyncio.Future()
    in_memory_after_future.set_result("8.0")  # Value after potential updates
    mock_dual_cache.in_memory_cache.async_get_cache.return_value = (
        in_memory_after_future
    )

    await base_strategy._sync_in_memory_spend_with_redis()

    # Verify the final merged values
    set_cache_calls = mock_dual_cache.in_memory_cache.async_set_cache.call_args_list
    print(f"set_cache_calls: {set_cache_calls}")
    assert any(
        call.kwargs["key"] == "key1" and float(call.kwargs["value"]) == 18.0
        for call in set_cache_calls
    )

    # A successful sync consumes the pending keys: the next tick must start from
    # an empty set instead of re-reading (and eventually only re-reading) every
    # key ever touched. Keys survive only when the sync raises.
    assert len(base_strategy.in_memory_keys_to_update) == 0


@pytest.mark.asyncio
async def test_cache_keys_management(base_strategy):
    # Test adding and getting cache keys
    base_strategy.add_to_in_memory_keys_to_update("key1")
    base_strategy.add_to_in_memory_keys_to_update("key2")
    base_strategy.add_to_in_memory_keys_to_update("key1")  # Duplicate should be ignored

    cache_keys = base_strategy.get_in_memory_keys_to_update()
    assert len(cache_keys) == 2
    assert "key1" in cache_keys
    assert "key2" in cache_keys

    # Test resetting cache keys
    base_strategy.reset_in_memory_keys_to_update()
    assert len(base_strategy.get_in_memory_keys_to_update()) == 0


@pytest.mark.asyncio
async def test_push_refused_by_the_open_circuit_breaker_is_not_logged_as_an_error(base_strategy, mock_dual_cache, caplog):
    """The sync loop pushes every 100 ms under usage-based routing, so an open breaker must not add an error line per cycle."""
    mock_dual_cache.redis_cache.async_increment_pipeline.side_effect = RedisCircuitBreakerOpenError(
        "Redis circuit breaker is open - skipping async_increment_pipeline"
    )
    base_strategy.redis_increment_operation_queue = [{"key": "k", "increment_value": 1.0, "ttl": 60}]

    with caplog.at_level(logging.ERROR):
        await base_strategy._push_in_memory_increments_to_redis()

    assert caplog.records == []
    assert base_strategy.redis_increment_operation_queue == []


def _sync_task_count() -> int:
    return sum(
        1
        for task in asyncio.all_tasks()
        if task.get_coro().__qualname__ == "BaseRoutingStrategy.periodic_sync_in_memory_spend_with_redis"
    )


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached before timeout"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_replacing_a_batching_strategy_stops_its_sync_task(mock_dual_cache):
    """Selector rebuilds must not strand the previous selector's sync task.

    Reproduces the prod OOM chain: every routing_strategy_args reconcile built a
    new LowestTPMLoggingHandler_v2 and left the old one's ``while True`` sync
    task running forever, accumulating ~3.2 immortal tasks/minute/worker until
    the pod OOMKilled (1126 live tasks observed in prod).
    """
    from litellm.router_strategy.lowest_tpm_rpm_v2 import LowestTPMLoggingHandler_v2

    first = LowestTPMLoggingHandler_v2(router_cache=mock_dual_cache, routing_args={})
    await _wait_until(lambda: _sync_task_count() == 1)

    first.dispose()

    await _wait_until(lambda: _sync_task_count() == 0)
    assert first._sync_task is None

    # dispose() is idempotent and safe on a fresh strategy with no task
    first.dispose()
    replacement = LowestTPMLoggingHandler_v2(router_cache=mock_dual_cache, routing_args={})
    replacement.dispose()
    await _wait_until(lambda: _sync_task_count() == 0)


@pytest.mark.asyncio
async def test_key_set_survives_failed_sync_and_caps_at_limit(mock_dual_cache):
    """Keys must survive a raising sync (retry next tick) but never grow unboundedly.

    The old sync read the non-resetting key set, so per-minute rpm/tpm keys
    accumulated for the life of the selector; the cap bounds the Redis-down
    window where every tick raises and the set can never drain.
    """
    from litellm.constants import ROUTING_STRATEGY_IN_MEMORY_KEYS_MAX

    strategy = BaseRoutingStrategy(
        dual_cache=mock_dual_cache,
        should_batch_redis_writes=False,
        default_sync_interval=1,
    )
    strategy.in_memory_keys_to_update = {"key1"}

    mock_dual_cache.in_memory_cache.async_batch_get_cache.side_effect = RuntimeError("redis down")
    # the sync swallows its own exceptions (logs them); the point is the keys
    # it had NOT yet flushed survive for the next tick
    await strategy._sync_in_memory_spend_with_redis()
    assert "key1" in strategy.in_memory_keys_to_update

    for i in range(ROUTING_STRATEGY_IN_MEMORY_KEYS_MAX + 5):
        strategy.add_to_in_memory_keys_to_update(f"key{i}")
    assert len(strategy.in_memory_keys_to_update) <= ROUTING_STRATEGY_IN_MEMORY_KEYS_MAX


@pytest.mark.asyncio
async def test_cleanup_disposes_the_sync_task(mock_dual_cache):
    """The pre-existing shutdown path must still work alongside dispose()."""
    strategy = BaseRoutingStrategy(
        dual_cache=mock_dual_cache,
        should_batch_redis_writes=True,
        default_sync_interval=1,
    )
    await _wait_until(lambda: _sync_task_count() == 1)

    await strategy.cleanup()

    await _wait_until(lambda: _sync_task_count() == 0)
    assert strategy._sync_task is None
