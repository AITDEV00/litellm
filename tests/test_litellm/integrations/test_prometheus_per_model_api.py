"""
Tests for the per-model metrics query helpers in prometheus_api.py.

These tests mock the HTTP calls to Prometheus and verify that the query
results are parsed correctly into the expected response shape.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_parse_window_to_timedelta_minutes():
    from litellm.integrations.prometheus_helpers.prometheus_api import (
        _parse_window_to_timedelta,
    )

    assert _parse_window_to_timedelta("1m") == timedelta(minutes=1)
    assert _parse_window_to_timedelta("15m") == timedelta(minutes=15)


def test_parse_window_to_timedelta_hours():
    from litellm.integrations.prometheus_helpers.prometheus_api import (
        _parse_window_to_timedelta,
    )

    assert _parse_window_to_timedelta("1h") == timedelta(hours=1)
    assert _parse_window_to_timedelta("24h") == timedelta(hours=24)


def test_parse_window_to_timedelta_days():
    from litellm.integrations.prometheus_helpers.prometheus_api import (
        _parse_window_to_timedelta,
    )

    assert _parse_window_to_timedelta("7d") == timedelta(days=7)


def test_parse_window_to_timedelta_invalid():
    from litellm.integrations.prometheus_helpers.prometheus_api import (
        _parse_window_to_timedelta,
    )

    with pytest.raises(ValueError):
        _parse_window_to_timedelta("unknown")


def test_extract_deployment_key():
    from litellm.integrations.prometheus_helpers.prometheus_api import (
        _extract_deployment_key,
    )

    labels = {
        "model_id": "abc-123",
        "api_base": "http://vllm:8000",
        "litellm_model_name": "Qwen3.6-35B",
        "api_provider": "hosted_vllm",
        "extra_label": "ignored",
    }
    key = _extract_deployment_key(labels)
    assert key == ("abc-123", "Qwen3.6-35B", "http://vllm:8000", "hosted_vllm")


def test_extract_deployment_key_missing_labels():
    from litellm.integrations.prometheus_helpers.prometheus_api import (
        _extract_deployment_key,
    )

    key = _extract_deployment_key({})
    assert key == ("", "", "", "")


def test_parse_range_result():
    from litellm.integrations.prometheus_helpers.prometheus_api import (
        _parse_range_result,
    )

    result = [
        {
            "metric": {"model_id": "abc-123"},
            "values": [
                [1700000000, "1.0"],
                [1700000030, "2.0"],
            ],
        }
    ]
    points = _parse_range_result(result)
    assert len(points) == 2
    assert points[0]["value"] == 1.0
    assert points[1]["value"] == 2.0
    assert "timestamp" in points[0]


@pytest.mark.asyncio
async def test_get_per_model_metrics_no_prometheus_url():
    """When PROMETHEUS_URL is not set, returns prometheus_connected=False."""
    from litellm.integrations.prometheus_helpers import prometheus_api

    with patch.object(prometheus_api, "PROMETHEUS_URL", None):
        result = await prometheus_api.get_per_model_metrics(window="1h")
    assert result["prometheus_connected"] is False
    assert result["deployments"] == []


@pytest.mark.asyncio
async def test_query_prometheus_range_sends_unix_timestamps():
    """query_prometheus_range must send Unix timestamps, not isoformat strings.

    Appending '+00:00' to an already timezone-aware datetime produces an
    invalid timestamp that Prometheus silently rejects, returning empty
    results.
    """
    from litellm.integrations.prometheus_helpers import prometheus_api

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"data": {"result": []}}

    start_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end_dt = datetime(2026, 1, 2, tzinfo=timezone.utc)

    with (
        patch.object(prometheus_api, "PROMETHEUS_URL", "http://prometheus:9090"),
        patch.object(prometheus_api.async_http_handler, "get", AsyncMock(return_value=mock_response)) as mock_get,
    ):
        await prometheus_api.query_prometheus_range("up", start_dt, end_dt, "30s")
        call_kwargs = mock_get.call_args
        params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
        assert isinstance(params["start"], float)
        assert isinstance(params["end"], float)
        assert params["start"] == start_dt.timestamp()
        assert params["end"] == end_dt.timestamp()


@pytest.mark.asyncio
async def test_get_per_model_metrics_with_mock_prometheus():
    """When Prometheus returns data, deployments are aggregated correctly."""
    from litellm.integrations.prometheus_helpers import prometheus_api

    range_response = [
        {
            "metric": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "litellm_model_name": "Qwen3.6-35B",
                "api_provider": "hosted_vllm",
            },
            "values": [[1700000000, "1"], [1700000030, "2"]],
        }
    ]
    instant_response = [
        {
            "metric": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "litellm_model_name": "Qwen3.6-35B",
                "api_provider": "hosted_vllm",
            },
            "value": [1700000000, "100"],
        }
    ]

    with (
        patch.object(prometheus_api, "PROMETHEUS_URL", "http://prometheus:9090"),
        patch.object(
            prometheus_api,
            "query_prometheus_range",
            AsyncMock(return_value=range_response),
        ),
        patch.object(
            prometheus_api,
            "query_prometheus_instant",
            AsyncMock(return_value=instant_response),
        ),
    ):
        result = await prometheus_api.get_per_model_metrics(window="1h")

    assert result["prometheus_connected"] is True
    assert result["window"] == "1h"
    assert result["step"] == "30s"
    assert len(result["deployments"]) == 1

    dep = result["deployments"][0]
    assert dep["model_id"] == "abc-123"
    assert dep["litellm_model_name"] == "Qwen3.6-35B"
    assert dep["rpm_limit"] == 100
    assert len(dep["concurrent_requests"]) == 2
    assert len(dep["request_rate"]) == 2
    assert len(dep["output_tokens_per_sec"]) == 2
    assert len(dep["latency_per_token_p50"]) == 2


@pytest.mark.asyncio
async def test_get_per_model_metrics_with_model_id_filter():
    """When model_id is provided, the PromQL includes a label filter."""
    from litellm.integrations.prometheus_helpers import prometheus_api

    captured_queries: list[str] = []

    async def mock_range(query, start, end, step):
        captured_queries.append(query)
        return []

    async def mock_instant(query):
        captured_queries.append(query)
        return []

    with (
        patch.object(prometheus_api, "PROMETHEUS_URL", "http://prometheus:9090"),
        patch.object(
            prometheus_api,
            "query_prometheus_range",
            side_effect=mock_range,
        ),
        patch.object(
            prometheus_api,
            "query_prometheus_instant",
            side_effect=mock_instant,
        ),
    ):
        result = await prometheus_api.get_per_model_metrics(
            window="1h", model_id="abc-123"
        )

    assert result["prometheus_connected"] is True
    assert any('model_id="abc-123"' in q for q in captured_queries)


@pytest.mark.asyncio
async def test_get_per_model_metrics_all_queries_group_by_same_labels():
    """Regression: output_tokens_per_sec must group by api_base too.

    If it omits api_base from the sum-by, _extract_deployment_key returns
    api_base="" for every output_tokens series, creating a phantom deployment
    instead of merging into the correct one.
    """
    from litellm.integrations.prometheus_helpers import prometheus_api

    captured_queries: list[str] = []

    async def mock_range(query, start, end, step):
        captured_queries.append(query)
        return []

    async def mock_instant(query):
        return []

    with (
        patch.object(prometheus_api, "PROMETHEUS_URL", "http://prometheus:9090"),
        patch.object(prometheus_api, "query_prometheus_range", side_effect=mock_range),
        patch.object(prometheus_api, "query_prometheus_instant", side_effect=mock_instant),
    ):
        await prometheus_api.get_per_model_metrics(window="1h")

    range_queries = [q for q in captured_queries if "sum by" in q]
    assert len(range_queries) == 3
    for q in range_queries:
        assert "api_base" in q, f"Query missing api_base in group-by: {q}"


@pytest.mark.asyncio
async def test_query_prometheus_range_raises_on_http_error():
    """query_prometheus_range must raise_for_status on non-2xx responses."""
    from litellm.integrations.prometheus_helpers import prometheus_api

    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.raise_for_status.side_effect = Exception("400 Bad Request")

    with (
        patch.object(prometheus_api, "PROMETHEUS_URL", "http://prometheus:9090"),
        patch.object(prometheus_api.async_http_handler, "get", AsyncMock(return_value=mock_response)),
    ):
        with pytest.raises(Exception, match="400"):
            await prometheus_api.query_prometheus_range(
                "up", datetime(2026, 1, 1), datetime(2026, 1, 2), "30s"
            )


@pytest.mark.asyncio
async def test_get_in_progress_requests_instant_no_url():
    from litellm.integrations.prometheus_helpers import prometheus_api

    with patch.object(prometheus_api, "PROMETHEUS_URL", None):
        result = await prometheus_api.get_in_progress_requests_instant()
    assert result == []


@pytest.mark.asyncio
async def test_get_in_progress_requests_instant_with_mock():
    from litellm.integrations.prometheus_helpers import prometheus_api

    instant_response = [
        {
            "metric": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "litellm_model_name": "Qwen3.6-35B",
                "api_provider": "hosted_vllm",
            },
            "value": [1700000000, "3"],
        }
    ]

    with (
        patch.object(prometheus_api, "PROMETHEUS_URL", "http://prometheus:9090"),
        patch.object(
            prometheus_api,
            "query_prometheus_instant",
            AsyncMock(return_value=instant_response),
        ),
    ):
        result = await prometheus_api.get_in_progress_requests_instant()

    assert len(result) == 1
    assert result[0]["model_id"] == "abc-123"
    assert result[0]["value"] == 3.0
