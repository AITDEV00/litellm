"""Tests for the per-deployment telemetry reader (``/endpoints`` telemetry).

Covers the PromQL construction, the response flattening (NaN + missing
model_id), the unit transforms (TTFT seconds -> ms, latency-per-token ->
tokens/sec), the "all four quantiles or nothing" rule, caching, and the
requested-subset filter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from unittest.mock import patch

import pytest

from litellm.proxy.openrouter_compat.enrichment import telemetry as dm
from litellm.proxy.openrouter_compat.enrichment.telemetry import (
    Percentiles,
    PrometheusDeploymentTelemetryReader,
    _percentiles_from,
    _series_by_model_id,
)

_UNCONFIGURED = object()


@pytest.fixture(autouse=True)
def _prometheus_configured():
    """Treat Prometheus as connected so ``read`` does not short-circuit.

    ``PROMETHEUS_URL`` is resolved from the environment at import time and is
    unset in tests; the reader guards on it, so tests that exercise ``read``
    need it non-None. Tests that assert the unconnected path override it.
    """
    with patch.object(dm, "PROMETHEUS_URL", "http://prometheus.test"):
        yield


def _instant(value: str, model_id: str) -> Mapping[str, object]:
    return {"metric": {"model_id": model_id}, "value": [0, value]}


def _reader(values: Mapping[str, Mapping[str, float]]) -> PrometheusDeploymentTelemetryReader:
    """A reader whose Prometheus client is a stub keyed by the metric in the query.

    The stub returns RAW result entries (as the real client does), so the
    reader's own flattening is exercised rather than bypassed.
    """

    async def _fake(query: str) -> Sequence[Mapping[str, object]]:
        for key, result in values.items():
            if key in query:
                return [_instant(str(v), mid) for mid, v in result.items()]
        return []

    return PrometheusDeploymentTelemetryReader(query=_fake)


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

    snapshot = await _reader(values)._fetch()

    m = snapshot["m1"]
    assert m.ttft_latency_ms == Percentiles(p50=500.0, p75=500.0, p90=500.0, p99=500.0)
    assert m.throughput_tokens_per_sec == Percentiles(p50=50.0, p75=50.0, p90=50.0, p99=50.0)
    # Prometheus returns floats; the DTO declares int | None, so it must be truncated.
    assert m.live_concurrency == 3
    assert isinstance(m.live_concurrency, int)
    assert m.requests_last_30m == 1234.0


@pytest.mark.asyncio
async def test_fetch_uptime_is_success_over_total_times_100():
    """Uptime comes from success/(success+failure)*100, computed per window."""
    values = {
        "success_responses_total[5m]": {"m1": 9.0},
        "failure_responses_total[5m]": {"m1": 1.0},
    }
    snapshot = await _reader(values)._fetch()
    assert snapshot["m1"].uptime_last_5m == pytest.approx(90.0)


@pytest.mark.asyncio
async def test_uptime_is_100_when_no_failures():
    """A deployment with successes and no failure series must report 100%, not None.

    Regression: a PromQL success/(success+failure) division against a missing
    failure series yields an empty vector, so the healthiest deployments lost
    their uptime entirely. The failure term must default to 0.
    """
    values = {"success_responses_total[30m]": {"m1": 5.0}}  # no failure series at all
    snapshot = await _reader(values)._fetch()
    assert snapshot["m1"].uptime_last_30m == 100.0


@pytest.mark.asyncio
async def test_uptime_is_none_when_no_traffic():
    """A deployment with no success series in the window has no uptime, not 0%."""
    values = {"litellm_deployment_in_progress_requests": {"m1": 0.0}}
    snapshot = await _reader(values)._fetch()
    assert snapshot["m1"].uptime_last_30m is None


@pytest.mark.asyncio
async def test_fetch_missing_metrics_are_none_not_zero():
    """A deployment present only in the concurrency query has null elsewhere."""
    values = {"litellm_deployment_in_progress_requests": {"m1": 2.0}}
    snapshot = await _reader(values)._fetch()
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

    async def _counting(query: str) -> Sequence[Mapping[str, object]]:
        calls.append(query)
        return []

    reader = PrometheusDeploymentTelemetryReader(query=_counting)
    await reader.read(["m1"])
    first = len(calls)
    await reader.read(["m1"])
    assert first == 16
    assert len(calls) == first, "the second read must hit the cache, not Prometheus"


@pytest.mark.asyncio
async def test_read_returns_only_requested_ids():
    values = {"litellm_deployment_in_progress_requests": {"m1": 1.0, "m2": 2.0}}
    result = await _reader(values).read(["m2"])
    assert set(result) == {"m2"}


@pytest.mark.asyncio
async def test_read_without_prometheus_returns_empty():
    with patch.object(dm, "PROMETHEUS_URL", None):
        assert await PrometheusDeploymentTelemetryReader().read(["m1"]) == {}


@pytest.mark.asyncio
async def test_read_with_no_ids_returns_empty():
    assert await PrometheusDeploymentTelemetryReader().read([]) == {}


@pytest.mark.asyncio
async def test_concurrent_reads_share_one_fetch():
    """Two simultaneous reads must not double the query load."""
    calls: list[str] = []

    async def _counting(query: str) -> Sequence[Mapping[str, object]]:
        calls.append(query)
        await asyncio.sleep(0.01)
        return []

    reader = PrometheusDeploymentTelemetryReader(query=_counting)
    await asyncio.gather(reader.read(["m1"]), reader.read(["m1"]))
    # 16 queries is one full fetch; a second concurrent fetch would double it.
    assert len(calls) == 16
