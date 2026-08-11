"""Behavior pins for ``model_performance_endpoints.py`` routes.

Pins:
    - GET /model/performance (happy: empty data list)
    - GET /model/performance (happy: with db rows)
    - GET /model/performance (error: invalid window)
    - GET /model/performance (error: prisma not initialized)
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from litellm.proxy import proxy_server


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def prisma_with_query_raw(monkeypatch):
    pc = MagicMock()
    pc.db.query_raw = AsyncMock(return_value=[])
    monkeypatch.setattr(proxy_server, "prisma_client", pc)
    return pc


@pytest.fixture
def no_prisma(monkeypatch):
    monkeypatch.setattr(proxy_server, "prisma_client", None)
    yield


# ---------------------------------------------------------------------------
# GET /model/performance — happy path (empty data)
# ---------------------------------------------------------------------------


def test_model_performance_happy_empty(client, auth_as, prisma_with_query_raw):
    """Pins ``GET /model/performance`` (happy: empty data list)."""
    with auth_as():
        response = client.get("/model/performance", params={"window": "1h"})
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "1h"
    assert body["models"] == []


# ---------------------------------------------------------------------------
# GET /model/performance — happy path (with db rows)
# ---------------------------------------------------------------------------


def test_model_performance_happy_with_rows(client, auth_as, monkeypatch):
    """Pins ``GET /model/performance`` (happy: with db rollup rows)."""
    pc = MagicMock()
    pc.db.query_raw = AsyncMock(
        return_value=[
            {
                "model_group": "gpt-4",
                "bucket_start": "2025-01-01T00:00:00+00:00",
                "request_count": 10,
                "completion_tokens": 1000,
                "throughput_tokens_sum": 1000.0,
                "ttft_seconds_sum": 5.0,
                "ttft_seconds_sum_sq": 0.0,
                "ttft_seconds_min": 0.4,
                "ttft_seconds_max": 0.6,
                "ttft_histogram_edges": [0.1, 1.0, 10.0, 100.0, float("inf")],
                "ttft_histogram_counts": [0, 10, 0, 0, 0],
                "starts": 10,
                "ends": 0,
            }
        ]
    )
    monkeypatch.setattr(proxy_server, "prisma_client", pc)
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.is_prometheus_connected",
        lambda: False,
    )
    with auth_as():
        response = client.get("/model/performance", params={"window": "1h"})
    assert response.status_code == 200
    body = response.json()
    assert body["window"] == "1h"
    assert body["source"] == "rollup"
    assert len(body["models"]) == 1
    assert body["models"][0]["model_group"] == "gpt-4"
    assert body["models"][0]["summary"]["total_requests"] == 10
    assert body["models"][0]["summary"]["total_tokens"] == 1000
    # concurrent_requests is the continuous running-sum peak. With a single
    # bucket that only has starts (10) and no ends, the peak is 10.
    assert body["models"][0]["time_series"]["concurrent_requests"][0]["value"] == 10.0
    # Summary p50/p95/p99 must come from the whole-window TTFT histogram, not
    # the per-bucket mean (0.5). All 10 requests land in bin [1.0, 10.0), so the
    # log10-interpolated percentiles are p50=10^0.5~3.16, p95~8.91, p99~9.77. If
    # a regression falls back to the bucket-mean series, these assertions fail.
    assert body["models"][0]["summary"]["p50_ttft"] == pytest.approx(3.162, abs=0.01)
    assert body["models"][0]["summary"]["p95_ttft"] == pytest.approx(8.912, abs=0.01)
    assert body["models"][0]["summary"]["p99_ttft"] == pytest.approx(9.772, abs=0.01)


# ---------------------------------------------------------------------------
# _rollup_minutes_to_model — peak concurrency via continuous running sum
# ---------------------------------------------------------------------------


def _minute_row(mg, ts, starts, ends, req_count):
    edges = [0.1, 1.0, 10.0, float("inf")]
    return {
        "model_group": mg,
        "bucket_start": ts,
        "request_count": req_count,
        "completion_tokens": 0,
        "throughput_tokens_sum": 0.0,
        "ttft_seconds_sum": 0.0,
        "ttft_seconds_sum_sq": 0.0,
        "ttft_seconds_min": None,
        "ttft_seconds_max": None,
        "ttft_histogram_edges": edges,
        "ttft_histogram_counts": [0] * len(edges),
        "starts": starts,
        "ends": ends,
    }


def test_rollup_peak_concurrency_is_continuous_running_sum():
    """The peak concurrency is a running sum over the whole window that is NOT
    reset at coarse-bucket boundaries, and within a minute it applies the net
    (starts - ends) rather than all-starts-first.

    Scenario: 3 requests start in minute 0 (concurrency 3). All 3 end in minute
    1 (concurrency drops to 0). A coarse bucket covering both minutes must
    report a peak of 3, not 0 (a per-bucket reset) and not an inflated value.
    """
    from litellm.proxy.model_metrics_endpoints.model_performance_endpoints import (
        _rollup_minutes_to_model,
    )

    start = datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
    minutes = [
        _minute_row("gpt-4", "2025-01-01T00:00:00+00:00", starts=3, ends=0, req_count=3),
        _minute_row("gpt-4", "2025-01-01T00:01:00+00:00", starts=0, ends=3, req_count=0),
    ]
    model = _rollup_minutes_to_model("gpt-4", minutes, start, start + datetime.timedelta(hours=1), "1 hour")
    series = model["time_series"]["concurrent_requests"]
    # The continuous running sum peaks at 3 (in the start minute) and never
    # resets to 0 because the -1 events arrive a minute later.
    assert [s["value"] for s in series] == [3.0]


def test_model_performance_scoped_by_team(client, auth_as, monkeypatch):
    """Entity-scoped requests must filter by team_id and always use the DB path."""
    pc = MagicMock()
    captured_params = {}

    async def fake_query_raw(sql, *args):
        captured_params["sql"] = sql
        captured_params["params"] = args
        return [
            {
                "model_group": "gpt-4",
                "bucket": "2025-01-01T00:00:00Z",
                "request_count": 5,
                "total_completion_tokens": 500,
                "avg_throughput_tokens_per_sec": 50.0,
                "avg_ttft_seconds": 0.4,
                "concurrent_requests": 1.0,
            }
        ]

    pc.db.query_raw = fake_query_raw
    monkeypatch.setattr(proxy_server, "prisma_client", pc)
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.is_prometheus_connected",
        lambda: True,
    )
    with auth_as():
        response = client.get("/model/performance", params={"window": "5m", "team_id": "team-abc"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "db"
    assert len(body["models"]) == 1
    # Even though window=5m is a Prometheus window, the entity-scoped request
    # must go through the DB path (never leak cross-entity Prometheus data).
    sql = captured_params["sql"]
    assert "COALESCE(\"team_id\", '') = $5::text" in sql
    assert captured_params["params"][4] == "team-abc"


# ---------------------------------------------------------------------------
# GET /model/performance — Prometheus path (readable names + aggregation)
# ---------------------------------------------------------------------------


def test_model_performance_prometheus_resolves_names(client, auth_as, monkeypatch):
    """Prometheus windows must surface readable model names, not raw UUIDs.

    Multiple deployments serving the same model (different ``model_id`` UUIDs)
    must be merged into one model group keyed by ``litellm_model_name``.
    """
    pc = MagicMock()
    pc.db.query_raw = AsyncMock(return_value=[])
    monkeypatch.setattr(proxy_server, "prisma_client", pc)
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.is_prometheus_connected",
        lambda: True,
    )

    # Two deployments (UUIDs) for the same model, plus a separate one.
    label_metadata = {
        "uuid-a": {"litellm_model_name": "gpt-4", "api_base": "a", "api_provider": "openai"},
        "uuid-b": {"litellm_model_name": "gpt-4", "api_base": "b", "api_provider": "openai"},
        "uuid-c": {"litellm_model_name": "claude-3", "api_base": "c", "api_provider": "anthropic"},
    }

    async def fake_get_deployment_label_metadata(label_filter):
        return label_metadata, {}

    async def fake_query_prometheus_range(promql, start, end, step):
        if "in_progress_requests" in promql:
            return [
                {"metric": {"model_id": "uuid-a"}, "values": [["1700000000", "2"], ["1700000060", "3"]]},
                {"metric": {"model_id": "uuid-b"}, "values": [["1700000000", "4"], ["1700000060", "5"]]},
                {"metric": {"model_id": "uuid-c"}, "values": [["1700000000", "1"], ["1700000060", "1"]]},
            ]
        # throughput + ttft return empty to keep the test focused
        return []

    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.query_prometheus_range",
        fake_query_prometheus_range,
    )
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api._get_deployment_label_metadata",
        fake_get_deployment_label_metadata,
    )

    with auth_as():
        response = client.get("/model/performance", params={"window": "15m"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "prometheus"
    groups = {m["model_group"]: m for m in body["models"]}
    assert set(groups) == {"gpt-4", "claude-3"}
    # Both deployments merged: 2+4 at first bucket, 3+5 at second.
    gpt_series = groups["gpt-4"]["time_series"]["concurrent_requests"]
    by_ts = {p["timestamp"]: p["value"] for p in gpt_series}
    assert list(by_ts.values()) == [6.0, 8.0]
    # No raw UUID leaks into model_group keys.
    assert "uuid-a" not in groups and "uuid-b" not in groups and "uuid-c" not in groups


# ---------------------------------------------------------------------------
# GET /model/performance — Prometheus path strips provider prefix
# ---------------------------------------------------------------------------


def test_model_performance_prometheus_strips_provider_prefix(client, auth_as, monkeypatch):
    """Prometheus windows must surface model groups without the provider prefix.

    The ``litellm_model_name`` label holds the deployment's configured model
    name (e.g. ``hosted_vllm/Qwen/...``) while the DB path reads ``model_group``
    from SpendLogs without the prefix. The Prometheus path must strip a known
    provider prefix so Live and historical views show identical names.
    """
    pc = MagicMock()
    pc.db.query_raw = AsyncMock(return_value=[])
    monkeypatch.setattr(proxy_server, "prisma_client", pc)
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.is_prometheus_connected",
        lambda: True,
    )

    label_metadata = {
        "uuid-a": {
            "litellm_model_name": "hosted_vllm/Qwen/Qwen3.5-122B-A10B-GPTQ-Int4",
            "api_base": "a",
            "api_provider": "hosted_vllm",
        },
    }

    async def fake_get_deployment_label_metadata(label_filter):
        return label_metadata, {}

    async def fake_query_prometheus_range(promql, start, end, step):
        if "in_progress_requests" in promql:
            return [{"metric": {"model_id": "uuid-a"}, "values": [["1700000000", "2"]]}]
        return []

    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.query_prometheus_range",
        fake_query_prometheus_range,
    )
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api._get_deployment_label_metadata",
        fake_get_deployment_label_metadata,
    )

    with auth_as():
        response = client.get("/model/performance", params={"window": "5m"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "prometheus"
    assert body["models"][0]["model_group"] == "Qwen/Qwen3.5-122B-A10B-GPTQ-Int4"


# ---------------------------------------------------------------------------
# GET /model/performance — negative concurrency is clamped to 0
# ---------------------------------------------------------------------------


def test_model_performance_concurrency_clamps_negatives(client, auth_as, monkeypatch):
    """Negative in-progress gauge values (from inc/dec desync across restarts)
    must never surface as negative concurrent requests in the response."""
    label_metadata = {
        "uuid-a": {"litellm_model_name": "gpt-4", "api_base": "a", "api_provider": "openai"},
    }

    async def fake_get_deployment_label_metadata(label_filter):
        return label_metadata, {}

    async def fake_query_prometheus_range(promql, start, end, step):
        if "in_progress_requests" in promql:
            # One deployment stuck negative (desynced), one healthily positive.
            return [
                {"metric": {"model_id": "uuid-a"}, "values": [["1700000000", "-1500"], ["1700000060", "-1498"]]},
                {"metric": {"model_id": "uuid-b"}, "values": [["1700000000", "3"], ["1700000060", "4"]]},
            ]
        return []

    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.query_prometheus_range",
        fake_query_prometheus_range,
    )
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api._get_deployment_label_metadata",
        fake_get_deployment_label_metadata,
    )
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.is_prometheus_connected",
        lambda: True,
    )

    with auth_as():
        response = client.get("/model/performance", params={"window": "5m"})
    assert response.status_code == 200
    body = response.json()
    groups = {m["model_group"]: m for m in body["models"]}
    # uuid-b (label metadata only knows uuid-a, so uuid-b keys by its own id).
    # Assert no model group ever exposes a negative concurrency value.
    for mg in groups.values():
        vals = [p["value"] for p in mg["time_series"]["concurrent_requests"]]
        assert all(v >= 0 for v in vals), f"{mg['model_group']} has negative concurrency: {vals}"


# ---------------------------------------------------------------------------
# GET /model/performance — DB path passes the step/bucket param through
# ---------------------------------------------------------------------------


def test_model_performance_step_passes_bucket_interval(client, auth_as, monkeypatch):
    """An explicit ``step`` must override the window's default bucket interval."""
    pc = MagicMock()
    captured = {}

    async def fake_query_raw(sql, *args):
        captured["sql"] = sql
        captured["args"] = args
        return []

    pc.db.query_raw = fake_query_raw
    monkeypatch.setattr(proxy_server, "prisma_client", pc)
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.is_prometheus_connected",
        lambda: False,
    )
    with auth_as():
        response = client.get("/model/performance", params={"window": "24h", "step": "2 hours"})
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "rollup"
    # The rollup read aggregates 1-minute rows up to the requested step.
    assert body["step"] == "2 hours"
    # The rollup SQL reads by bucket_start and binds only start/end/model_group.
    assert "bucket_start" in captured["sql"]
    assert "LiteLLM_ModelPerformanceRollup" in captured["sql"]
    assert len(captured["args"]) == 3


# ---------------------------------------------------------------------------
# GET /model/performance — error: invalid window
# ---------------------------------------------------------------------------


def test_model_performance_invalid_window(client, auth_as, prisma_with_query_raw):
    """Pins ``GET /model/performance`` (error: invalid window)."""
    with auth_as():
        response = client.get("/model/performance", params={"window": "2h"})
    assert response.status_code == 400
    assert "invalid" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# GET /model/performance — error: prisma not initialized
# ---------------------------------------------------------------------------


def test_model_performance_no_prisma_error(client, auth_as, no_prisma, monkeypatch):
    """Pins ``GET /model/performance`` (error: prisma not initialized)."""
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.is_prometheus_connected",
        lambda: False,
    )
    with auth_as():
        response = client.get("/model/performance")
    assert response.status_code == 500
    assert response.content


# ---------------------------------------------------------------------------
# GET /model/performance — explicit start_time/end_time range
# ---------------------------------------------------------------------------


def test_model_performance_custom_time_range(client, auth_as, monkeypatch):
    """An explicit start_time/end_time must force the DB path (even for a
    Prometheus window) and pass the range through to the SQL query so the
    shared "Select Time Range" widget is honored."""
    pc = MagicMock()
    captured_params = {}

    async def fake_query_raw(sql, *args):
        captured_params["sql"] = sql
        captured_params["params"] = args
        return []

    pc.db.query_raw = fake_query_raw
    monkeypatch.setattr(proxy_server, "prisma_client", pc)
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.is_prometheus_connected",
        lambda: True,
    )
    start = "2025-06-01T00:00:00Z"
    end = "2025-06-03T00:00:00Z"
    with auth_as():
        response = client.get(
            "/model/performance",
            params={"window": "5m", "start_time": start, "end_time": end},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "rollup"
    # The rollup read clamps the range to the requested start/end.
    sql = captured_params["sql"]
    assert "bucket_start" in sql and "LiteLLM_ModelPerformanceRollup" in sql
    assert captured_params["params"][0] == datetime.datetime(2025, 6, 1, tzinfo=datetime.timezone.utc)
    assert captured_params["params"][1] == datetime.datetime(2025, 6, 3, tzinfo=datetime.timezone.utc)


def test_model_performance_query_avoids_base_cte_rescan(client, auth_as, monkeypatch):
    """The global view reads the rollup table, not the raw spend log.

    Previously the endpoint scanned ``LiteLLM_SpendLogs`` over large custom
    ranges (YTD over millions of rows), which was the dominant cost. The global
    path now reads the tiny 1-minute rollup slice instead, so the raw-table CTE
    with ``SUM(change) OVER`` no longer runs for a no-entity-filter request.
    """
    pc = MagicMock()
    captured_params = {}

    async def fake_query_raw(sql, *args):
        captured_params["sql"] = sql
        captured_params["params"] = args
        return []

    pc.db.query_raw = fake_query_raw
    monkeypatch.setattr(proxy_server, "prisma_client", pc)
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.is_prometheus_connected",
        lambda: True,
    )
    with auth_as():
        response = client.get(
            "/model/performance",
            params={"window": "7d", "start_time": "2025-01-01T00:00:00Z", "end_time": "2025-01-08T00:00:00Z"},
        )
    assert response.status_code == 200
    sql = captured_params["sql"]

    # Global view reads the rollup table, not the raw spend log.
    assert "LiteLLM_ModelPerformanceRollup" in sql
    assert "LiteLLM_SpendLogs" not in sql
    # Concurrency is recomputed at read from the rollup's starts/ends, so the
    # raw spend-log window-function machinery is gone from the global path.
    assert "SUM(change) OVER" not in sql


# GET /model/performance — a large custom range routes through the long-timeout client


def test_model_performance_large_range_uses_long_timeout_client(client, auth_as, monkeypatch):
    """A custom range of at least 14 days must route the DB query through the
    dedicated long-timeout Prisma client, so the 30s default HTTP timeout does
    not kill the query over millions of spend-log rows."""
    pc = MagicMock()

    async def shared_query_raw(sql, *args):
        raise AssertionError("shared proxy client must not run a >=14d range query")

    pc.db.query_raw = shared_query_raw
    monkeypatch.setattr(proxy_server, "prisma_client", pc)
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.is_prometheus_connected",
        lambda: True,
    )
    captured = {}
    heavy_client = MagicMock()

    async def heavy_query_raw(sql, *args):
        captured["sql"] = sql
        return []

    heavy_client.db.query_raw = heavy_query_raw
    monkeypatch.setattr(
        "litellm.proxy.model_metrics_endpoints.model_performance_endpoints._get_heavy_query_prisma_client",
        AsyncMock(return_value=heavy_client),
    )
    start = "2025-01-01T00:00:00Z"
    end = "2025-02-01T00:00:00Z"  # 31 days
    with auth_as():
        response = client.get(
            "/model/performance",
            params={"window": "7d", "start_time": start, "end_time": end},
        )
    assert response.status_code == 200
    assert response.json()["source"] == "rollup"
    # The heavy client (not the shared proxy client) must have executed the query.
    assert "bucket_start" in captured["sql"]
    assert "LiteLLM_ModelPerformanceRollup" in captured["sql"]
