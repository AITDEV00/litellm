"""Tests for the gateway status reader (checklist Steps 15-16).

Regression focus: the health table is append-only and its query filters on
``model_name``, while a logical model's status is per deployment. Reading the
wrong key would silently yield no status at all, so the join is asserted
directly.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

import pytest

from litellm.proxy.db.health_check_latest import LatestHealthCheckRow
from litellm.proxy.openrouter_compat.status_reader import GatewayStatusReader

_NOW: Final = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)

_MODEL_DETAILS: Final = {
    "v": 1,
    "status": "Ready",
    "cluster": "alain",
    "replicas": {"desired": 1, "available": 1},
    "error_msg": None,
    "observed_at": "2026-10-07T11:55:00+00:00",
    "gateway_uuid": "b6d6aaac-93e8-4ac0-943b-778d75869b05",
    "serving_available": True,
    "status_changed_at": "2026-10-06T17:42:19+00:00",
}


def _row(
    *,
    model_name: str,
    model_id: str | None,
    details: object = None,
    status: str = "healthy",
    checked_at: datetime = _NOW,
) -> LatestHealthCheckRow:
    return LatestHealthCheckRow(
        health_check_id="hc-1",
        model_name=model_name,
        model_id=model_id,
        status=status,
        details=details,
        checked_by="oicm-controller",
        checked_at=checked_at,
        created_at=checked_at,
        updated_at=checked_at,
    )


class _FakePrisma:
    """Stands in for the Prisma client so the reader's two reads are observable."""

    def __init__(self, rows: list[LatestHealthCheckRow]) -> None:
        self._rows = rows

    @property
    def db(self) -> object:
        return object()


async def _read(rows: list[LatestHealthCheckRow], *, model_name: str, deployment_ids: list[str]):
    reader = GatewayStatusReader(prisma_client=None)
    # Patch the module-level helpers rather than the client: the point under
    # test is which names the reader asks for, not how Prisma answers.
    import litellm.proxy.openrouter_compat.status_reader as mod

    async def fake_for_models(_prisma, names):
        return [r for r in rows if r.model_name in names and r.model_id is not None]

    async def fake_all(_prisma):
        return [r for r in rows if r.model_id is None]

    original_for_models, original_all = mod.fetch_latest_health_checks_for_models, mod.fetch_latest_health_checks
    mod.fetch_latest_health_checks_for_models = fake_for_models
    mod.fetch_latest_health_checks = fake_all
    try:
        reader._prisma = _FakePrisma(rows)  # type: ignore[assignment]  # fake stands in for the client
        return await reader.read(model_name=model_name, deployment_ids=deployment_ids)
    finally:
        mod.fetch_latest_health_checks_for_models = original_for_models
        mod.fetch_latest_health_checks = original_all


@pytest.mark.asyncio
async def test_status_is_matched_by_model_id_not_model_name():
    """Two deployments of one logical model each get their own status."""
    rows = [
        _row(model_name="Qwen/Qwen3.5-122B", model_id="dep-a", details=_MODEL_DETAILS),
        _row(model_name="Qwen/Qwen3.5-122B", model_id="dep-b", details=_MODEL_DETAILS),
        _row(model_name="oicm-source-alain", model_id=None, checked_at=_NOW),
    ]
    inputs = await _read(rows, model_name="Qwen/Qwen3.5-122B", deployment_ids=["dep-a", "dep-b"])

    assert set(inputs) == {"dep-a", "dep-b"}


@pytest.mark.asyncio
async def test_query_uses_the_logical_model_name():
    """Passing deployment ids to the name-filtered query would match nothing."""
    rows = [
        _row(model_name="Qwen/Qwen3.5-122B", model_id="dep-a", details=_MODEL_DETAILS),
        _row(model_name="oicm-source-alain", model_id=None, checked_at=_NOW),
    ]
    inputs = await _read(rows, model_name="Qwen/Qwen3.5-122B", deployment_ids=["dep-a"])

    assert "dep-a" in inputs


@pytest.mark.asyncio
async def test_unmanaged_deployment_has_no_status():
    """A deployment with no health row must be absent, not defaulted to online."""
    rows = [
        _row(model_name="Qwen/Qwen3.5-122B", model_id="dep-a", details=_MODEL_DETAILS),
        _row(model_name="oicm-source-alain", model_id=None, checked_at=_NOW),
    ]
    inputs = await _read(rows, model_name="Qwen/Qwen3.5-122B", deployment_ids=["dep-a", "dep-unmanaged"])

    assert set(inputs) == {"dep-a"}


@pytest.mark.asyncio
async def test_details_are_parsed_into_gateway_inputs():
    rows = [
        _row(model_name="Qwen/Qwen3.5-122B", model_id="dep-a", details=_MODEL_DETAILS),
        _row(model_name="oicm-source-alain", model_id=None, checked_at=_NOW),
    ]
    inputs = await _read(rows, model_name="Qwen/Qwen3.5-122B", deployment_ids=["dep-a"])
    value = inputs["dep-a"]

    assert value.oicm_status == "Ready"
    assert value.serving_available is True
    assert value.replicas_desired == 1
    assert value.replicas_available == 1
    assert value.cluster == "alain"
    assert value.health_status == "healthy"
    assert value.source_checked_at == _NOW


@pytest.mark.asyncio
async def test_source_heartbeat_is_found_by_cluster_from_details():
    """The heartbeat lookup is keyed on the cluster the model row records."""
    rows = [
        _row(model_name="Qwen/Qwen3.5-122B", model_id="dep-a", details=_MODEL_DETAILS),
        _row(model_name="oicm-source-abudhabi", model_id=None, checked_at=_NOW),
    ]
    inputs = await _read(rows, model_name="Qwen/Qwen3.5-122B", deployment_ids=["dep-a"])

    assert inputs["dep-a"].source_checked_at is None


@pytest.mark.asyncio
async def test_missing_details_yields_unknown_not_a_crash():
    rows = [
        _row(model_name="Qwen/Qwen3.5-122B", model_id="dep-a", details=None),
        _row(model_name="oicm-source-alain", model_id=None, checked_at=_NOW),
    ]
    inputs = await _read(rows, model_name="Qwen/Qwen3.5-122B", deployment_ids=["dep-a"])
    value = inputs["dep-a"]

    assert value.oicm_status is None
    assert value.serving_available is None
    assert value.replicas_desired is None


@pytest.mark.asyncio
async def test_no_prisma_client_returns_no_status():
    reader = GatewayStatusReader(prisma_client=None)
    assert await reader.read(model_name="Qwen/Qwen3.5-122B", deployment_ids=["dep-a"]) == {}


@pytest.mark.asyncio
async def test_empty_deployment_ids_skips_the_query():
    reader = GatewayStatusReader(prisma_client=None)
    assert await reader.read(model_name="Qwen/Qwen3.5-122B", deployment_ids=[]) == {}
