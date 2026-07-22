"""
Tests for the /model/metrics/per_model endpoint.

Tests window validation, Prometheus-connected path, and fallback path.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _create_app():
    app = FastAPI()
    from litellm.proxy.model_metrics_endpoints.per_model_endpoints import router

    app.include_router(router)
    return app


def test_per_model_metrics_invalid_window():
    app = _create_app()
    client = TestClient(app)

    with patch("litellm.proxy.auth.user_api_key_auth.user_api_key_auth"):
        response = client.get(
            "/model/metrics/per_model",
            params={"window": "invalid"},
        )
    assert response.status_code == 400
    assert "Invalid window" in response.json()["detail"]


def test_per_model_metrics_valid_windows_accepted():
    app = _create_app()
    client = TestClient(app)

    async def mock_get_per_model_metrics(window, model_id=None):
        return {
            "prometheus_connected": True,
            "window": window,
            "step": "30s",
            "deployments": [],
        }

    with (
        patch("litellm.proxy.auth.user_api_key_auth.user_api_key_auth"),
        patch(
            "litellm.proxy.model_metrics_endpoints.per_model_endpoints.is_prometheus_connected",
            return_value=True,
        ),
        patch(
            "litellm.proxy.model_metrics_endpoints.per_model_endpoints.fetch_per_model_metrics",
            new=mock_get_per_model_metrics,
        ),
    ):
        for window in ("1m", "15m", "1h", "24h", "7d"):
            response = client.get(
                "/model/metrics/per_model",
                params={"window": window},
            )
            assert response.status_code == 200, f"window={window} failed"
            assert response.json()["window"] == window


def test_per_model_metrics_fallback_when_prometheus_not_connected():
    app = _create_app()
    client = TestClient(app)

    instant_data = [
        {
            "model_id": "abc-123",
            "litellm_model_name": "Qwen3.6-35B",
            "api_base": "http://vllm:8000",
            "api_provider": "hosted_vllm",
            "value": 3.0,
        }
    ]

    with (
        patch("litellm.proxy.auth.user_api_key_auth.user_api_key_auth"),
        patch(
            "litellm.proxy.model_metrics_endpoints.per_model_endpoints.is_prometheus_connected",
            return_value=False,
        ),
        patch(
            "litellm.proxy.model_metrics_endpoints.per_model_endpoints.get_in_progress_requests_instant",
            new_callable=AsyncMock,
            return_value=instant_data,
        ),
    ):
        response = client.get(
            "/model/metrics/per_model",
            params={"window": "1h"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["prometheus_connected"] is False
    assert len(data["deployments"]) == 1
    assert data["deployments"][0]["model_id"] == "abc-123"
    assert data["deployments"][0]["concurrent_requests"][0]["value"] == 3.0
    assert data["deployments"][0]["request_rate"] == []


def test_per_model_metrics_passes_model_id_filter():
    app = _create_app()
    client = TestClient(app)

    mock_data = {
        "prometheus_connected": True,
        "window": "1h",
        "step": "30s",
        "deployments": [],
    }

    with (
        patch("litellm.proxy.auth.user_api_key_auth.user_api_key_auth"),
        patch(
            "litellm.proxy.model_metrics_endpoints.per_model_endpoints.is_prometheus_connected",
            return_value=True,
        ),
        patch(
            "litellm.proxy.model_metrics_endpoints.per_model_endpoints.fetch_per_model_metrics",
            new_callable=AsyncMock,
            return_value=mock_data,
        ) as mock_fn,
    ):
        response = client.get(
            "/model/metrics/per_model",
            params={"window": "1h", "model_id": "abc-123"},
        )

    assert response.status_code == 200
    mock_fn.assert_called_once_with(window="1h", model_id="abc-123")
