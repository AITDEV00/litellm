"""Read the gateway-observed status facts for a set of deployments.

Two bounded reads, both keyed on what is actually being served:

- the latest native health row per deployment id, which carries the OICM
  lifecycle block in ``details`` and the native healthy/unhealthy verdict;
- the latest heartbeat per OICM source, which is what freshness is judged on.

The health table is append-only, so "latest" is resolved in Postgres by the
existing ``fetch_latest_health_checks`` helpers rather than by streaming history
into the worker.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from litellm._logging import verbose_proxy_logger
from litellm.proxy.db.health_check_latest import (
    LatestHealthCheckRow,
    fetch_latest_health_checks,
    fetch_latest_health_checks_for_models,
)
from litellm.proxy.openrouter_compat.gateway_status import GatewayStatusInputs
from litellm.proxy.utils import PrismaClient

_SOURCE_ROW_PREFIX: Final = "oicm-source-"


class GatewayStatusReader:
    """Resolve ``GatewayStatusInputs`` per deployment id for one request."""

    def __init__(self, prisma_client: PrismaClient | None) -> None:
        self._prisma = prisma_client

    async def read(
        self,
        *,
        model_name: str,
        deployment_ids: Sequence[str],
    ) -> Mapping[str, GatewayStatusInputs]:
        """Inputs for each deployment of ``model_name`` that has a health row.

        The query filters on ``model_name`` (the helper's only filter) but the
        result is keyed by ``model_id``: a logical model with several
        deployments has one health row per deployment, all sharing a name.
        """
        if self._prisma is None or not deployment_ids:
            return {}
        rows = await fetch_latest_health_checks_for_models(self._prisma, [model_name])
        by_id = {row.model_id: row for row in rows if row.model_id is not None}
        heartbeats = await self._read_heartbeats()
        return {
            deployment_id: self._inputs(by_id[deployment_id], heartbeats)
            for deployment_id in deployment_ids
            if deployment_id in by_id
        }

    async def _read_heartbeats(self) -> Mapping[str, LatestHealthCheckRow]:
        """Latest row per OICM source, keyed by cluster name."""
        if self._prisma is None:
            return {}
        try:
            rows = await fetch_latest_health_checks(self._prisma)
        except Exception as query_err:  # noqa: BLE001  # status decoration must not fail the endpoint
            verbose_proxy_logger.error("Error reading OICM source heartbeats: %s", query_err)
            return {}
        return {
            row.model_name.removeprefix(_SOURCE_ROW_PREFIX): row
            for row in rows
            if row.model_id is None and row.model_name.startswith(_SOURCE_ROW_PREFIX)
        }

    @staticmethod
    def _inputs(
        row: LatestHealthCheckRow,
        heartbeats: Mapping[str, LatestHealthCheckRow],
    ) -> GatewayStatusInputs:
        details = row.details if isinstance(row.details, dict) else {}
        cluster = _str_or_none(details.get("cluster"))
        heartbeat = heartbeats.get(cluster) if cluster else None
        replicas = details.get("replicas")
        replica_map = replicas if isinstance(replicas, dict) else {}
        return GatewayStatusInputs(
            oicm_status=_str_or_none(details.get("status")),
            serving_available=_bool_or_none(details.get("serving_available")),
            replicas_desired=_int_or_none(replica_map.get("desired")),
            replicas_available=_int_or_none(replica_map.get("available")),
            observed_at=_str_or_none(details.get("observed_at")),
            cluster=cluster,
            health_status=row.status,
            source_checked_at=heartbeat.checked_at if heartbeat else None,
        )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _bool_or_none(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
