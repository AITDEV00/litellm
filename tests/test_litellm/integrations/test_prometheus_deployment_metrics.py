"""Tests for the per-deployment telemetry reader (``/endpoints`` telemetry).

Covers the PromQL construction, the response flattening (NaN + missing
model_id), the unit transforms (TTFT seconds -> ms, latency-per-token ->
tokens/sec), the "all four quantiles or nothing" rule, caching, and the
requested-subset filter.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from litellm.integrations.prometheus_helpers import deployment_metrics as dm
from litellm.integrations.prometheus_helpers.deployment_metrics import (
    DeploymentMetricsReader,
    Percentiles,
    _percentiles_from,
    _series_by_model_id,
)


@pytest.fixture(autouse=True)
def _prometheus_configured():
    """Treat Prometheus as connected so ``read`` does not short-circuit.

    ``PROMETHEUS_URL`` is resolved from the environment at import time and is
    unset in tests; the reader guards on it, so tests that exercise ``read``
    need it non-None. Tests that assert the unconnected path override it.
    """
    with patch.object(dm, "PROMETHEUS_URL", "http://prometheus.test"):
        yield


def _instant(value: str, model_id: str) -> dict:
    return {"metric": {"model_id": model_id}, "value": [0, value]}


def test_series_by_model_id_drops_nan_and_empty_ids():
    raw = [
        _instant("1.5", "a"),
        _instant("NaN", "b"),
        _instant("2.0", ""),
        _instant("3.0", "c"),
    ]
    assert _series_by_model_id(raw) == {"a": 1.5, "c": 3.0}


def test_percentiles_from_requires_all_four():
    """A partial distribution is absent, never fabricated from fewer quantiles."""
    assert _percentiles_from({"p50": 1.0, "p75": 2.0}, lambda v: v) is None
    full = _percentiles_from({"p50": 1.0, "p75": 2.0, "p90": 3.0, "p99": 4.0}, lambda v: v * 10)
    assert full == Percentiles(p50=10.0, p75=20.0, p90=30.0, p99=40.0)


def _fake_query_factory(values: dict[str, dict[str, float]]):
    """Return an async query stub keyed by the metric name embedded in the query.

    The stub returns RAW Prometheus result entries (as the real client does), so
    the reader's own flattening is exercised rather than bypassed.
    """

    async def _fake(query: str) -> list[dict]:
        for key, result in values.items():
            if key in query:
                return [_instant(str(v), mid) for mid, v in result.items()]
        return []

    return _fake


@pytest.mark.asyncio
async def test_fetch_builds_metrics_with_correct_units():
    """TTFT is seconds->ms; latency-per-token is inverted to tokens/second."""
    values = {
        "litellm_llm_api_time_to_first_token_metric": {"m1": 0.5},  # seconds
        "litellm_deployment_latency_per_output_token": {"m1": 0.02},  # seconds/token
        "success_responses_total[30m]": {"m1": 99.0},
        "failure_responses_total[30m]": {"m1": 1.0},
        "litellm_deployment_in_progress_requests": {"m1": 3.0},
        "litellm_deployment_total_requests_total[30m]": {"m1": 1234.0},
    }

    with patch.object(dm, "query_prometheus_instant", side_effect=_fake_query_factory(values)):
        snapshot = await DeploymentMetricsReader()._fetch()

    m = snapshot["m1"]
    assert m.ttft_latency_ms == Percentiles(p50=500.0, p75=500.0, p90=500.0, p99=500.0)
    assert m.throughput_tokens_per_sec == Percentiles(p50=50.0, p75=50.0, p90=50.0, p99=50.0)
    assert m.live_concurrency == 3
    assert m.requests_last_30m == 1234.0


@pytest.mark.asyncio
async def test_fetch_uptime_is_success_over_total_times_100():
    """Uptime comes from success/(success+failure)*100 per window."""
    # The uptime query combines both counters in one expression, so it is keyed
    # by its distinctive trailing ``* 100`` rather than either counter name.
    values = {"* 100": {"m1": 90.0}}
    with patch.object(dm, "query_prometheus_instant", side_effect=_fake_query_factory(values)):
        snapshot = await DeploymentMetricsReader()._fetch()
    assert snapshot["m1"].uptime_last_5m == pytest.approx(90.0)
    assert snapshot["m1"].uptime_last_30m == pytest.approx(90.0)
    assert snapshot["m1"].uptime_last_1d == pytest.approx(90.0)


@pytest.mark.asyncio
async def test_fetch_missing_metrics_are_none_not_zero():
    """A deployment present only in the concurrency query has null elsewhere."""
    values = {"litellm_deployment_in_progress_requests": {"m1": 2.0}}
    with patch.object(dm, "query_prometheus_instant", side_effect=_fake_query_factory(values)):
        snapshot = await DeploymentMetricsReader()._fetch()
    m = snapshot["m1"]
    assert m.live_concurrency == 2
    assert m.ttft_latency_ms is None
    assert m.throughput_tokens_per_sec is None
    assert m.uptime_last_30m is None
    assert m.requests_last_30m is None


@pytest.mark.asyncio
async def test_snapshot_is_cached_within_ttl():
    """A second read inside the TTL must not re-query Prometheus."""
    calls: list[str] = []

    async def _counting(query: str) -> list[dict]:
        calls.append(query)
        return []

    with patch.object(dm, "query_prometheus_instant", side_effect=_counting):
        reader = DeploymentMetricsReader()
        await reader.read(["m1"])
        first = len(calls)
        await reader.read(["m1"])
    assert first == 13
    assert len(calls) == first, "the second read must hit the cache, not Prometheus"


@pytest.mark.asyncio
async def test_read_returns_only_requested_ids():
    values = {"litellm_deployment_in_progress_requests": {"m1": 1.0, "m2": 2.0}}
    with patch.object(dm, "query_prometheus_instant", side_effect=_fake_query_factory(values)):
        result = await DeploymentMetricsReader().read(["m2"])
    assert set(result) == {"m2"}


@pytest.mark.asyncio
async def test_read_without_prometheus_returns_empty():
    with patch.object(dm, "PROMETHEUS_URL", None):
        assert await DeploymentMetricsReader().read(["m1"]) == {}
@pytest.mark.asyncio
async def test_read_with_no_ids_returns_empty():
    assert await DeploymentMetricsReader().read([]) == {}


@pytest.mark.asyncio
async def test_concurrent_reads_share_one_fetch():
    """Two simultaneous reads must not double the query load."""
    calls: list[str] = []

    async def _counting(query: str) -> list[dict]:
        calls.append(query)
        await asyncio.sleep(0.01)
        return []

    reader = DeploymentMetricsReader()
    with patch.object(dm, "query_prometheus_instant", side_effect=_counting):
        await asyncio.gather(reader.read(["m1"]), reader.read(["m1"]))
    # 13 queries is one full fetch; a second concurrent fetch would double it.
    assert len(calls) == 13
