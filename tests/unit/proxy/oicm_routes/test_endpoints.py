"""Tests for the OICM status ingestion routes (litellm/proxy/oicm_routes.py).

Pins behavior, not structure: reports land in the native health save function
with the native status vocabulary, the native loop's row shape is produced,
heartbeats cannot collide with any model row, and non-admins are refused.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.oicm_routes import (
    _MAX_BATCH_SIZE,
    OicmHeartbeatBatch,
    OicmSourceHeartbeat,
    OicmStatusReportBatch,
    oicm_heartbeats,
    oicm_status_reports,
    router,
)


def _admin() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(api_key="test-key", user_role=LitellmUserRoles.PROXY_ADMIN)


def _internal_user() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(api_key="test-key", user_role=LitellmUserRoles.INTERNAL_USER)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[user_api_key_auth] = lambda: _admin()
    return app


def _client() -> TestClient:
    return TestClient(_app())


_REPORTS_BODY = {
    "reports": [
        {
            "model_name": "zai-org/GLM-5.2-FP8",
            "litellm_model_id": "766b1720-f516-4077-b22c-6ce97c045470",
            "healthy": True,
            "details": {
                "status": "Ready",
                "serving_available": True,
                "cluster": "abudhabi",
            },
        },
        {
            "model_name": "Qwen/Qwen3-Next-80B-A3B-Instruct",
            "litellm_model_id": "2557a581-4429-4e58-bad8-6ff9d4202970",
            "healthy": False,
            "error_message": "no ready replicas",
            "details": {"status": "Ready", "serving_available": False},
        },
    ]
}


class TestStatusReports:
    @pytest.mark.asyncio
    async def test_each_report_becomes_one_native_save(self):
        """Two reports must reach the native write function twice, once each."""
        prisma = MagicMock()
        prisma.save_health_check_result = AsyncMock(return_value=object())

        with patch(
            "litellm.proxy.proxy_server.prisma_client", prisma
        ):
            result = await oicm_status_reports(
                OicmStatusReportBatch(**_REPORTS_BODY), user_api_key_dict=_admin()
            )

        assert prisma.save_health_check_result.await_count == 2
        assert result == {"saved": 2, "received": 2}

    @pytest.mark.asyncio
    async def test_status_uses_the_native_vocabulary(self):
        """`healthy` maps to the two strings the UI and the loop branch on.

        Writing OICM's literal word ("Ready") would render as an error in the
        Admin UI, which treats anything other than "healthy" as a failure.
        """
        prisma = MagicMock()
        prisma.save_health_check_result = AsyncMock(return_value=object())

        with patch(
            "litellm.proxy.proxy_server.prisma_client", prisma
        ):
            await oicm_status_reports(
                OicmStatusReportBatch(**_REPORTS_BODY), user_api_key_dict=_admin()
            )

        statuses = [
            c.kwargs["status"] for c in prisma.save_health_check_result.await_args_list
        ]
        assert statuses == ["healthy", "unhealthy"]

    @pytest.mark.asyncio
    async def test_serving_truth_rides_in_details_not_status(self):
        """The OICM lifecycle word must be preserved, inside `details`."""
        prisma = MagicMock()
        prisma.save_health_check_result = AsyncMock(return_value=object())

        with patch(
            "litellm.proxy.proxy_server.prisma_client", prisma
        ):
            await oicm_status_reports(
                OicmStatusReportBatch(**_REPORTS_BODY), user_api_key_dict=_admin()
            )

        first = prisma.save_health_check_result.await_args_list[0].kwargs
        assert first["details"]["status"] == "Ready"
        assert first["details"]["cluster"] == "abudhabi"
        second = prisma.save_health_check_result.await_args_list[1].kwargs
        assert second["error_message"] == "no ready replicas"

    @pytest.mark.asyncio
    async def test_no_response_time_is_written(self):
        """OICM reports no latency; null keeps the loop's rows comparable."""
        prisma = MagicMock()
        prisma.save_health_check_result = AsyncMock(return_value=object())

        with patch(
            "litellm.proxy.proxy_server.prisma_client", prisma
        ):
            await oicm_status_reports(
                OicmStatusReportBatch(**_REPORTS_BODY), user_api_key_dict=_admin()
            )

        assert all(
            c.kwargs["response_time_ms"] is None
            for c in prisma.save_health_check_result.await_args_list
        )

    @pytest.mark.asyncio
    async def test_failed_saves_are_counted_not_raised(self):
        """`save_health_check_result` never raises; its None return is the failure.

        A failed insert must not break the rest of the batch.
        """
        prisma = MagicMock()
        prisma.save_health_check_result = AsyncMock(side_effect=[None, object()])

        with patch(
            "litellm.proxy.proxy_server.prisma_client", prisma
        ):
            result = await oicm_status_reports(
                OicmStatusReportBatch(**_REPORTS_BODY), user_api_key_dict=_admin()
            )

        assert result == {"saved": 1, "received": 2}

    @pytest.mark.asyncio
    async def test_non_admin_is_refused(self):
        from fastapi import HTTPException

        prisma = MagicMock()
        prisma.save_health_check_result = AsyncMock(return_value=object())

        with patch(
            "litellm.proxy.proxy_server.prisma_client", prisma
        ):
            with pytest.raises(HTTPException) as exc:
                await oicm_status_reports(
                    OicmStatusReportBatch(**_REPORTS_BODY),
                    user_api_key_dict=_internal_user(),
                )

        assert exc.value.status_code == 403
        prisma.save_health_check_result.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reporter_stamps_checked_by(self):
        """Multiple writers must stay distinguishable in /health/latest."""
        prisma = MagicMock()
        prisma.save_health_check_result = AsyncMock(return_value=object())

        with patch(
            "litellm.proxy.proxy_server.prisma_client", prisma
        ):
            await oicm_status_reports(
                OicmStatusReportBatch(reporter="oicm-controller-ad", **_REPORTS_BODY),
                user_api_key_dict=_admin(),
            )

        assert all(
            c.kwargs["checked_by"] == "oicm-controller-ad"
            for c in prisma.save_health_check_result.await_args_list
        )

    def test_empty_batch_is_rejected_at_validation(self):
        client = _client()
        resp = client.post("/oicm/v1/status-reports", json={"reports": []})
        assert resp.status_code == 422

    def test_oversized_batch_is_rejected_at_validation(self):
        """A batch cap stops a buggy caller materializing an unbounded gather.

        The controller sends tens of entries per cycle, so the cap only ever
        fires on a caller bug, and it must reject rather than attempt the write.
        """
        client = _client()
        reports = [
            {"model_name": f"m{i}", "healthy": True}
            for i in range(_MAX_BATCH_SIZE + 1)
        ]
        resp = client.post("/oicm/v1/status-reports", json={"reports": reports})
        assert resp.status_code == 422

    def test_serving_false_is_not_required_to_carry_an_error(self):
        client = _client()
        with patch(
            "litellm.proxy.proxy_server.prisma_client", MagicMock()
        ) as prisma:
            prisma.save_health_check_result = AsyncMock(return_value=object())
            resp = client.post(
                "/oicm/v1/status-reports",
                json={"reports": [{"model_name": "m", "healthy": False}]},
            )
        assert resp.status_code == 200


class TestHeartbeats:
    @pytest.mark.asyncio
    async def test_heartbeat_row_cannot_be_a_model_row(self):
        """No model_id and a reserved name: no native writer produces this key."""
        prisma = MagicMock()
        prisma.save_health_check_result = AsyncMock(return_value=object())

        with patch(
            "litellm.proxy.proxy_server.prisma_client", prisma
        ):
            await oicm_heartbeats(
                OicmHeartbeatBatch(heartbeats=[OicmSourceHeartbeat(cluster="alain")]),
                user_api_key_dict=_admin(),
            )

        call = prisma.save_health_check_result.await_args
        assert call.kwargs["model_id"] is None
        assert call.kwargs["model_name"] == "oicm-source-alain"

    @pytest.mark.asyncio
    async def test_two_sources_write_two_rows(self):
        prisma = MagicMock()
        prisma.save_health_check_result = AsyncMock(return_value=object())

        with patch(
            "litellm.proxy.proxy_server.prisma_client", prisma
        ):
            result = await oicm_heartbeats(
                OicmHeartbeatBatch(
                    heartbeats=[
                        OicmSourceHeartbeat(cluster="alain"),
                        OicmSourceHeartbeat(cluster="abudhabi"),
                    ]
                ),
                user_api_key_dict=_admin(),
            )

        assert result == {"saved": 2, "received": 2}
        names = [c.kwargs["model_name"] for c in prisma.save_health_check_result.await_args_list]
        assert names == ["oicm-source-alain", "oicm-source-abudhabi"]

    def test_unauthenticated_call_is_refused(self):
        app = _app()
        app.dependency_overrides.clear()
        client = TestClient(app)
        resp = client.post("/oicm/v1/heartbeats", json={"heartbeats": [{"cluster": "alain"}]})
        assert resp.status_code in (401, 403)


class TestRouteShape:
    def test_paths_live_in_the_oicm_namespace(self):
        """The route paths must stay under /oicm, upstream's unreachable prefix.

        If a path ever moves under /v1 or /health, an upstream route can
        silently shadow it (FastAPI matches first-registered).
        """
        import litellm.proxy.oicm_routes as mod

        registered = {route.path for route in mod.router.routes}
        assert registered == {
            "/oicm/v1/status-reports",
            "/oicm/v1/heartbeats",
        }
