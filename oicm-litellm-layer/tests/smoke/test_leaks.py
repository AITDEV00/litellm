"""Tier C: leak and boundedness checks. Slower, no inference.

Deterministic counts, not RSS trends: a single request burst cannot surface the
OOM classes documented in docs/performance. What IS deterministic is that the
asyncio task census stays bounded, and that the in-memory caches report sane
counts.

The default ``smoke-leaks`` target is READ-ONLY. The config-reload mutation that
exercises the v1.102 sync-task leak lives behind ``smoke-leaks-mutate`` because
it changes a live router setting (and ``POST /config/update`` persists to the
DB), so it must never fire as a side effect of a routine smoke run.
"""

from __future__ import annotations

import time

import pytest

from .client import Gateway, Success, error_detail

pytestmark = pytest.mark.soak

CONFIG_RELOADS = 5
SETTLE_SECONDS = 5
TASK_GROWTH_TOLERANCE = 50


def _task_count(gw: Gateway) -> int:
    result = gw.get("/debug/asyncio-tasks")
    assert isinstance(result, Success), f"cannot read asyncio-tasks: {error_detail(result)}"
    return int(result.body["total_active_tasks"])


def _current_router_values(gw: Gateway) -> dict:
    """/router/settings nests the live values under ``current_values``."""
    result = gw.get("/router/settings")
    assert isinstance(result, Success), f"cannot read router settings: {error_detail(result)}"
    values = result.body.get("current_values")
    assert isinstance(values, dict), "router settings missing 'current_values'"
    return values


class TestTaskCensusReadable:
    def test_census_returns_a_sane_count(self, gw: Gateway) -> None:
        count = _task_count(gw)
        assert count > 0, f"task census {count} suggests the endpoint is broken"
        assert count < 100_000, f"task census {count} is implausibly high"


class TestCacheBoundedness:
    def test_caches_report_counts(self, gw: Gateway) -> None:
        result = gw.get("/debug/memory/summary")
        assert isinstance(result, Success), f"cannot read memory summary: {error_detail(result)}"
        caches = result.body.get("caches")
        assert isinstance(caches, dict), "memory summary missing caches"
        for name in ("user_api_keys", "llm_responses", "usage_tracking"):
            if name in caches:
                count = caches[name].get("count")
                assert isinstance(count, int), f"cache {name} count not int"
                assert count >= 0


@pytest.mark.mutating
class TestTaskLeakCensus:
    def test_routing_args_reloads_do_not_leak_tasks(self, gw: Gateway) -> None:
        """Change ``routing_strategy_args`` N times, restore it, census must settle.

        Regression test for the v1.102 sync-task leak: a routing-strategy rebuild
        leaked the previous selector's periodic task. ``update_settings`` only
        rebuilds on a *real* ``routing_strategy_args`` change, so a no-op write
        would not exercise the path. ``routing_strategy_args`` is DB-owned (so the
        write is accepted, unlike config-owned keys) and the original value is
        restored and re-verified in a finally block.
        """
        baseline = _task_count(gw)
        original = _current_router_values(gw).get("routing_strategy_args")
        assert isinstance(original, dict), "routing_strategy_args is not a dict"

        probe_value = {**original, "_smoke_probe": "1"}
        try:
            for _ in range(CONFIG_RELOADS):
                write = gw.post("/config/update", json={"router_settings": {"routing_strategy_args": probe_value}})
                assert isinstance(write, Success), f"config update failed: {error_detail(write)}"
            time.sleep(SETTLE_SECONDS)
            after = _task_count(gw)
            assert after <= baseline + TASK_GROWTH_TOLERANCE, (
                f"task census grew {baseline} -> {after} after {CONFIG_RELOADS} routing-args "
                f"reloads (tolerance {TASK_GROWTH_TOLERANCE}); possible periodic-task leak"
            )
        finally:
            restore = gw.post("/config/update", json={"router_settings": {"routing_strategy_args": original}})
            assert isinstance(restore, Success), f"FAILED TO RESTORE routing_strategy_args: {error_detail(restore)}"
            restored = _current_router_values(gw).get("routing_strategy_args")
            assert restored == original, f"routing_strategy_args not restored: {restored} != {original}"
