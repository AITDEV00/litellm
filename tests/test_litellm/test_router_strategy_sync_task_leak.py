"""
Regression tests for the routing-strategy sync-task leak that OOMKilled prod
(2026-09-30 diagnosis; see /memories/repo/routing-strategy-sync-task-leak.md).

Leak chain, for the record:

- ``BaseRoutingStrategy.__init__`` spawns an immortal
  ``periodic_sync_in_memory_spend_with_redis`` task (0.1s interval under
  usage-based-routing-v2).
- Every config/model reconcile calls ``Router.update_settings`` with the DB
  overlay, whose router_settings row ALWAYS carries ``routing_strategy_args``
  (even when it is ``{}``). The old code set ``routing_args_updated = True``
  unconditionally for that key, so every reconcile rebuilt the strategy
  selector and spawned a fresh sync task; the discarded selector's task was
  never cancelled (``cleanup()`` had zero callers).
- Prod measurement: ~3.2 stranded tasks/minute/worker; a 4.7h-old worker held
  1126 of them and 3.8GB RSS while ``gc_objects`` stayed flat, ratcheting the
  pod to its 12Gi limit every ~3.5-4.5h.

The fix under test: rebuild only on a real args change, and dispose the
replaced selector's sync task in ``_unregister_router_selectors`` (the single
choke point every selector-discard path flows through).
"""

import asyncio
from typing import Final
from unittest.mock import MagicMock

import pytest

import litellm
from litellm.caching.caching import DualCache
from litellm.router import Router
from litellm.router_strategy.budget_limiter import RouterBudgetLimiting


SYNC_TASK_QUALNAME: Final = "BaseRoutingStrategy.periodic_sync_in_memory_spend_with_redis"
BUDGET_SYNC_TASK_QUALNAME: Final = "RouterBudgetLimiting.periodic_sync_in_memory_spend_with_redis"


@pytest.fixture(autouse=True)
def isolate_litellm_callbacks():
    callbacks_before: Final = litellm.callbacks.copy()
    yield
    litellm.callbacks = callbacks_before  # test-quality-ok: required callback-state restoration fixture


def _make_router() -> Router:
    """Build the router INSIDE the running test loop: the selector's sync task
    is loop-bound at construction (BaseRoutingStrategy.setup_sync_task), so a
    loop-less fixture construction would park it on an unstarted new loop."""
    return Router(
        model_list=[
            {
                "model_name": "test-model",
                "litellm_params": {"model": "openai/gpt-4o-mini"},
            }
        ],
        routing_strategy="usage-based-routing-v2",
        router_general_settings={"async_only_mode": True},
    )


async def _dispose_router_callbacks(router: Router) -> None:
    selector: Final = getattr(router, "lowesttpm_logger_v2", None)
    if selector is not None and hasattr(selector, "dispose"):
        selector.dispose()
    for cb in (router.optional_callbacks or []):
        dispose: Final = getattr(cb, "dispose", None)
        if dispose is not None:
            dispose()


def _task_count(qualname: str) -> int:
    return sum(1 for task in asyncio.all_tasks() if task.get_coro().__qualname__ == qualname)


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline: Final = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached before timeout"
        await asyncio.sleep(0.01)


def _mock_dual_cache() -> MagicMock:
    dual_cache = MagicMock(spec=DualCache)
    dual_cache.in_memory_cache = MagicMock()
    dual_cache.redis_cache = MagicMock()
    dual_cache.redis_cache.async_increment_pipeline.return_value = _completed(None)
    dual_cache.in_memory_cache.async_batch_get_cache.return_value = _completed([])
    dual_cache.in_memory_cache.async_get_cache.return_value = _completed(0)
    dual_cache.in_memory_cache.async_set_cache.return_value = _completed(None)
    return dual_cache


def _completed(value: object) -> asyncio.Future:
    future: Final = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


@pytest.mark.asyncio
async def test_update_settings_with_identical_args_keeps_selector_identity():
    """The DB overlay always carries routing_strategy_args, so a reconcile that
    passes the current args back must be a no-op: same selector object, no
    rebuild. This is the exact call shape prod fired every ~19 seconds."""
    router: Final = _make_router()
    try:
        original_selector: Final = router.lowesttpm_logger_v2
        assert original_selector is not None

        # the prod call shape: the DB overlay echoes the stored args back,
        # including the empty-dict default
        router.update_settings(routing_strategy_args=router.routing_strategy_args)

        assert router.lowesttpm_logger_v2 is original_selector
    finally:
        await _dispose_router_callbacks(router)


@pytest.mark.asyncio
async def test_repeated_reconciles_do_not_strand_sync_tasks():
    """N no-op reconciles must leave exactly one live sync task: the selector's own.

    Before the fix this stranded one immortal 10Hz task per reconcile,
    ~3.2/minute/worker in prod, which OOMKilled the pods every few hours.
    """
    router: Final = _make_router()
    try:
        await _wait_until(lambda: _task_count(SYNC_TASK_QUALNAME) >= 1)
        baseline: Final = _task_count(SYNC_TASK_QUALNAME)

        for _ in range(10):
            router.update_settings(routing_strategy_args={})
            await asyncio.sleep(0.05)

        # allow any cancelled task a moment to finish unwinding
        await _wait_until(lambda: _task_count(SYNC_TASK_QUALNAME) == baseline)

        assert _task_count(SYNC_TASK_QUALNAME) <= baseline + 1
    finally:
        await _dispose_router_callbacks(router)


@pytest.mark.asyncio
async def test_changed_args_rebuild_disposes_previous_sync_task():
    """A genuine args change must rebuild (frozen RoutingArgs) but dispose the
    replaced selector's sync task, so exactly one task survives the swap."""
    router: Final = _make_router()
    try:
        before: Final = _task_count(SYNC_TASK_QUALNAME)
        assert before >= 1, "router must have started its selector's sync task"

        router.update_settings(routing_strategy_args={"ttl": 999})

        await _wait_until(lambda: _task_count(SYNC_TASK_QUALNAME) <= before)
        assert router.lowesttpm_logger_v2 is not None
        assert router.lowesttpm_logger_v2.routing_args.ttl == 999
    finally:
        await _dispose_router_callbacks(router)


@pytest.mark.asyncio
async def test_routing_strategy_init_disposes_previous_selector():
    """A strategy switch replaces the selector; the old one's sync task must not survive it."""
    router: Final = _make_router()
    try:
        await _wait_until(lambda: _task_count(SYNC_TASK_QUALNAME) >= 1)

        router.routing_strategy_init(routing_strategy="least-busy", routing_strategy_args={})

        # least-busy registers an input_callback selector, not a BaseRoutingStrategy,
        # so the v2 sync tasks must all be gone
        await _wait_until(lambda: _task_count(SYNC_TASK_QUALNAME) == 0)
    finally:
        await _dispose_router_callbacks(router)


@pytest.mark.asyncio
async def test_budget_limiter_task_is_cancellable_and_disposed():
    """RouterBudgetLimiting spawns its sync task with no reference at all (the
    worst variant of the leak: unreferenced, hence un-cancellable). It must now
    hold the task and dispose() must stop it."""
    dual_cache: Final = _mock_dual_cache()
    limiter: Final = RouterBudgetLimiting(
        dual_cache=dual_cache,
        provider_budget_config={"openai": {"budget_limit": 10, "time_period": "1d"}},
    )
    await _wait_until(lambda: _task_count(BUDGET_SYNC_TASK_QUALNAME) >= 1)

    limiter.dispose()

    await _wait_until(lambda: _task_count(BUDGET_SYNC_TASK_QUALNAME) == 0)
    assert limiter._sync_task is None


@pytest.mark.asyncio
async def test_remove_optional_callbacks_disposes_budget_limiter():
    """The global-cleanup branch of _remove_optional_callbacks_of_type (the last
    live router dropping the callback type) must stop the limiter's sync task,
    not just remove the callback from the list."""
    from litellm.router_strategy.budget_limiter import RouterBudgetLimiting
    from litellm.types.router import Deployment, LiteLLM_Params, ModelInfo

    router: Final = Router(
        model_list=[],
        optional_pre_call_checks=[],
        router_general_settings={"async_only_mode": True},
    )
    router.add_deployment(
        deployment=Deployment(
            model_name="dynamic-budget-model",
            litellm_params=LiteLLM_Params(
                model="openai/gpt-4o-mini",
                api_key="fake-key",
                max_budget=0.000000000001,
                budget_duration="1d",
            ),
            model_info=ModelInfo(id="runtime-budget-deployment"),
        )
    )
    limiter: Final = router._get_router_deployment_budget_limiter()
    assert limiter is not None
    await _wait_until(lambda: _task_count(BUDGET_SYNC_TASK_QUALNAME) >= 1)

    router._remove_optional_callbacks_of_type(RouterBudgetLimiting)

    await _wait_until(lambda: _task_count(BUDGET_SYNC_TASK_QUALNAME) == 0)
    assert limiter._sync_task is None
