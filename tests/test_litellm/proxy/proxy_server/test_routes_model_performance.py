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
    """Pins ``GET /model/performance`` (happy: with db rows)."""
    pc = MagicMock()
    pc.db.query_raw = AsyncMock(
        return_value=[
            {
                "model_group": "gpt-4",
                "bucket": "2025-01-01T00:00:00Z",
                "request_count": 10,
                "total_completion_tokens": 1000,
                "avg_throughput_tokens_per_sec": 100.0,
                "avg_ttft_seconds": 0.5,
                "concurrent_requests": 2.0,
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
    assert len(body["models"]) == 1
    assert body["models"][0]["model_group"] == "gpt-4"
    assert body["models"][0]["summary"]["total_requests"] == 10
    assert body["models"][0]["summary"]["total_tokens"] == 1000
    # concurrent_requests must reflect the real value, not a hardcoded 0
    assert body["models"][0]["summary"]["avg_concurrent"] == 2.0
    assert body["models"][0]["time_series"]["concurrent_requests"][0]["value"] == 2.0


# ---------------------------------------------------------------------------
# GET /model/performance — entity scoping (team_id)
# ---------------------------------------------------------------------------


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
        return [
            {
                "model_group": "gpt-4",
                "bucket": "2025-01-01T00:00:00Z",
                "request_count": 10,
                "total_completion_tokens": 1000,
                "avg_throughput_tokens_per_sec": 100.0,
                "avg_ttft_seconds": 0.5,
                "concurrent_requests": 2.0,
            }
        ]

    pc.db.query_raw = fake_query_raw
    monkeypatch.setattr(proxy_server, "prisma_client", pc)
    monkeypatch.setattr(
        "litellm.integrations.prometheus_helpers.prometheus_api.is_prometheus_connected",
        lambda: False,
    )
    with auth_as():
        response = client.get("/model/performance", params={"window": "24h", "step": "2 hours"})
    assert response.status_code == 200
    assert response.json()["source"] == "db"
    # The SQL is built with $3 as the bucket interval; assert the override won.
    assert "$3::interval" in captured["sql"]
    # Positional arg 3 is the bucket interval; it must be the user-supplied step,
    # NOT the window default ("1 hour" for 24h).
    assert captured["args"][2] == "2 hours"


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
    assert body["source"] == "db"
    # The captured range must be clamped to the requested start/end.
    sql = captured_params["sql"]
    assert "startTime" in sql and "endTime" in sql
    assert captured_params["params"][0] == datetime.datetime(2025, 6, 1, tzinfo=datetime.timezone.utc)
    assert captured_params["params"][1] == datetime.datetime(2025, 6, 3, tzinfo=datetime.timezone.utc)


def test_model_performance_query_avoids_base_cte_rescan(client, auth_as, monkeypatch):
    """Large-range performance query must not re-scan a shared ``base`` CTE.

    The original query factored the time-range filter into a ``base`` CTE that
    was referenced multiple times, which made Postgres materialize the row set
    once and then re-read it per reference. On a YTD range over millions of
    spend-log rows that re-materialization was the dominant cost. The optimized
    query aggregates straight from the table in each CTE.

    Concurrency is computed as the PEAK within each bucket: each request emits a
    +1 at its start and a -1 at its end, a running SUM of those changes yields
    the simultaneously-active count at every instant, and the max inside a
    bucket is that bucket's peak concurrency.
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

    # There must be no shared ``base`` CTE: the query must aggregate from the
    # table directly, otherwise Postgres materializes + re-scans it per ref.
    assert "WITH base AS" not in sql
    assert "FROM \"LiteLLM_SpendLogs\"" in sql
    # Concurrency is the peak per bucket: a running SUM over +/-1 start/end
    # events (window function), then MAX within each date_bin bucket.
    assert "SUM(change) OVER" in sql
    assert "GREATEST(MAX(cumulative), 0)" in sql
    # Requests that started before the window (whose endTime is inside it) must
    # contribute their -1, so we clamp the end event to the window edge.
    assert "LEAST(\"endTime\", $2::timestamptz)" in sql
    # Every bucketed row still carries the concurrent-requests series.
    assert "COALESCE(c.concurrent_requests, 0)" in sql


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
    assert response.json()["source"] == "db"
    # The heavy client (not the shared proxy client) must have executed the query.
    assert "startTime" in captured["sql"]
