"""OICM status-ingestion routes.

Self-contained vertical slice, co-located next to ``proxy_server.py`` like
``voice_routes.py`` so upstream merges never touch it. The router is mounted in
the fork-appended include block at the bottom of ``proxy_server.py``.

The OICM discovery controller polls its platform's REST API, which is the
authoritative source for whether a deployment can serve, and reports what it
saw here. This fills ``LiteLLM_HealthCheckTable`` with OICM truth instead of
probe truth, reusing the native write function, native read endpoints
(``/health/latest``, ``/health/history``), native retention and the Admin UI
health column unchanged.

Semantics kept identical to the native background health-check loop:

- ``status`` uses the native two-word vocabulary, ``healthy`` / ``unhealthy``,
  because the Admin UI branches on ``status == "healthy"`` and the loop's
  write-on-change comparison reads the same field. The OICM lifecycle word
  (``Ready``, ``Stopped``...) rides inside ``details``.
- Rows are plain inserts into an append-only history table; the controller
  decides when a write is warranted, mirroring the native rule of on-change
  plus a periodic refresh.

Nothing here can affect routing: the router's health state cache is populated
only by the background probe loop, never from this table.
"""

from __future__ import annotations

import asyncio
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from litellm._logging import verbose_proxy_logger
from litellm.proxy._types import LitellmUserRoles
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth

router = APIRouter(tags=["oicm"])

_HEALTHY: Final = "healthy"
_UNHEALTHY: Final = "unhealthy"

_DEFAULT_CHECKED_BY: Final = "oicm-controller"
_SOURCE_ROW_PREFIX: Final = "oicm-source-"

# Caps one batch so a buggy caller cannot make the proxy materialize an
# unbounded gather. The controller sends one entry per deployment (tens), so
# this is far above any real cycle; it matches the house batch cap used
# elsewhere in the proxy.
_MAX_BATCH_SIZE: Final = 500


class OicmStatusReport(BaseModel):
    """One model's observed status, mapped for the native health table.

    ``healthy`` derives from OICM ``serving_available`` (not the lifecycle
    word): a ``Ready`` deployment with no serving pods is unhealthy here, the
    same verdict a probe would return.
    """

    model_config = ConfigDict(protected_namespaces=())

    model_name: str = Field(min_length=1)
    litellm_model_id: str | None = None
    healthy: bool
    error_message: str | None = None
    details: dict[str, object] | None = None


class OicmStatusReportBatch(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    reporter: str = _DEFAULT_CHECKED_BY
    reports: list[OicmStatusReport] = Field(min_length=1, max_length=_MAX_BATCH_SIZE)


class OicmSourceHeartbeat(BaseModel):
    """Liveness for one OICM source: its cluster was polled, now, successfully."""

    model_config = ConfigDict(protected_namespaces=())

    cluster: str = Field(min_length=1)


class OicmHeartbeatBatch(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    reporter: str = _DEFAULT_CHECKED_BY
    heartbeats: list[OicmSourceHeartbeat] = Field(min_length=1, max_length=_MAX_BATCH_SIZE)


def _require_admin(user_api_key_dict: UserAPIKeyAuth) -> None:
    if user_api_key_dict.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only proxy admins can report OICM status",
        )


def _require_prisma(prisma_client: object) -> None:
    if prisma_client is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "Database not connected"},
        )


@router.post(
    "/oicm/v1/status-reports",
    dependencies=[Depends(user_api_key_auth)],
)
async def oicm_status_reports(
    batch: OicmStatusReportBatch,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> dict[str, object]:
    """Persist one health row per report, into the native health table."""
    from litellm.proxy.proxy_server import prisma_client  # noqa: PLC0415  # proxy_server imports this router, so defer

    _require_admin(user_api_key_dict)
    _require_prisma(prisma_client)

    writes = tuple(
        prisma_client.save_health_check_result(
            model_name=report.model_name,
            model_id=report.litellm_model_id,
            status=_HEALTHY if report.healthy else _UNHEALTHY,
            healthy_count=1 if report.healthy else 0,
            unhealthy_count=0 if report.healthy else 1,
            error_message=report.error_message,
            response_time_ms=None,
            details=report.details,
            checked_by=batch.reporter,
        )
        for report in batch.reports
    )

    rows = await asyncio.gather(*writes)
    saved = sum(1 for r in rows if r is not None)
    if saved != len(batch.reports):
        verbose_proxy_logger.warning(
            "oicm status-reports: saved %d/%d rows", saved, len(batch.reports)
        )
    return {"saved": saved, "received": len(batch.reports)}


@router.post(
    "/oicm/v1/heartbeats",
    dependencies=[Depends(user_api_key_auth)],
)
async def oicm_heartbeats(
    batch: OicmHeartbeatBatch,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> dict[str, object]:
    """Persist one liveness row per source.

    The row has no ``model_id`` and a ``model_name`` no deployment can share,
    so no native writer can collide with it and the Admin UI's
    latest-health-checks join skips it (no model id matches).
    """
    from litellm.proxy.proxy_server import prisma_client  # noqa: PLC0415  # proxy_server imports this router, so defer

    _require_admin(user_api_key_dict)
    _require_prisma(prisma_client)

    writes = tuple(
        prisma_client.save_health_check_result(
            model_name=f"{_SOURCE_ROW_PREFIX}{hb.cluster}",
            model_id=None,
            status=_HEALTHY,
            healthy_count=1,
            unhealthy_count=0,
            error_message=None,
            response_time_ms=None,
            details=None,
            checked_by=batch.reporter,
        )
        for hb in batch.heartbeats
    )

    rows = await asyncio.gather(*writes)
    saved = sum(1 for r in rows if r is not None)
    if saved != len(batch.heartbeats):
        verbose_proxy_logger.warning(
            "oicm heartbeats: saved %d/%d rows", saved, len(batch.heartbeats)
        )
    return {"saved": saved, "received": len(batch.heartbeats)}
