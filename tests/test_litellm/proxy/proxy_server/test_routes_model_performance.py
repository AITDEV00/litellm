"""Behavior pins for ``model_performance_endpoints.py`` routes.

Pins:
    - GET /model/performance (happy: empty data list)
    - GET /model/performance (happy: with db rows)
    - GET /model/performance (error: invalid window)
    - GET /model/performance (error: prisma not initialized)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from litellm.proxy import proxy_server

from .conftest import normalize  # type: ignore[import-not-found]


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
                "concurrent_requests": 2.0,
                "throughput_tokens_per_sec": 100.0,
                "ttft_seconds": 0.5,
                "total_requests": 10,
                "total_tokens": 1000,
            }
        ]
    )
    monkeypatch.setattr(proxy_server, "prisma_client", pc)
    monkeypatch.setattr(
        "litellm.proxy.model_metrics_endpoints.model_performance_endpoints.is_prometheus_connected",
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
        "litellm.proxy.model_metrics_endpoints.model_performance_endpoints.is_prometheus_connected",
        lambda: False,
    )
    with auth_as():
        response = client.get("/model/performance")
    assert response.status_code == 500
    assert response.content
