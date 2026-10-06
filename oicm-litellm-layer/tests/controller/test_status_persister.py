"""Tests for persisting OICM status onto the LiteLLM rows.

Pins behavior, not structure: a status change must reach the gateway exactly
once, an unchanged cycle must write nothing, a lone `observed_at` must not be
mistaken for a change, the block must be complete, and liveness must be written
per source rather than per model.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from controller.litellm_client import gateway_uuid, heartbeat_payload
from controller.status.snapshot import DeploymentStatus, OicmStatusSnapshot
from controller.status_persister import (
    HEARTBEAT_NAME_PREFIX,
    StatusPersister,
    build_block,
    plan_writes,
)

def _snapshot(
    workload_id="dep1",
    status="Ready",
    serving=True,
    cluster="alain",
    observed_at="2026-10-06T00:00:00+00:00",
    status_changed_at="2026-10-06T00:00:00+00:00",
    error_msg=None,
    desired=1,
    available=1,
):
    return OicmStatusSnapshot(
        workspace_id="ws1",
        workload_id=workload_id,
        cluster=cluster,
        source_status=DeploymentStatus(status) if status else None,
        desired_replicas=desired,
        available_replicas=available,
        unavailable_replicas=None,
        serving_available=serving,
        error_msg=error_msg,
        source_updated_at=None,
        previous_source_status=None,
        status_changed_at=status_changed_at,
        observed_at=observed_at,
    )


def _entry(model_id="id-1", oicm=None, blocked=False):
    info = {"id": model_id, "oicm_uuid": "dep1", "blocked": blocked}
    if oicm is not None:
        info["oicm"] = oicm
    return {
        "model_id": model_id,
        "model_name": "hosted_vllm/test-model",
        "litellm_params": {"model": "hosted_vllm/test-model"},
        "model_info": info,
    }


class TestBlockShape:
    def test_block_is_complete_and_excludes_api_base(self):
        """The block carries every fact, and no api_base.

        LiteLLM merges `model_info` shallowly, so a partial block would replace
        the `oicm` object and drop what it omitted. `api_base` is deliberately
        absent: `litellm_params.api_base` already survives on the row and is what
        routing reads, so a second copy could only drift.
        """
        block = build_block(_snapshot())

        assert set(block) == {
            "v",
            "status",
            "serving_available",
            "cluster",
            "gateway_uuid",
            "replicas",
            "status_changed_at",
            "observed_at",
            "error_msg",
        }
        assert block["v"] == 1
        assert block["replicas"] == {"desired": 1, "available": 1}
        assert block["cluster"] == "alain"
        assert block["gateway_uuid"] == "dep1"

    def test_error_msg_is_stored_in_full(self):
        """A long diagnostic must not be truncated: a partial message is worse."""
        long_msg = "x" * 4000

        assert build_block(_snapshot(error_msg=long_msg))["error_msg"] == long_msg

    def test_stopped_reports_not_serving(self):
        block = build_block(_snapshot(status="Stopped", serving=False))

        assert block["status"] == "Stopped"
        assert block["serving_available"] is False


class TestWriteGuard:
    def test_first_observation_writes(self):
        """A row with no block yet must be written, or status never lands."""
        writes = plan_writes({"dep1": _snapshot()}, {"dep1": [_entry()]})

        assert len(writes) == 1
        assert writes[0].litellm_model_id == "id-1"
        assert writes[0].blocked is False

    def test_identical_cycle_writes_nothing(self):
        """Steady state must be a no-op.

        Every gateway write reloads every model on every replica, so a cycle that
        finds the same facts has to issue nothing at all.
        """
        stored = build_block(_snapshot())

        writes = plan_writes(
            {"dep1": _snapshot()}, {"dep1": [_entry(oicm=stored, blocked=False)]}
        )

        assert writes == ()

    def test_observed_at_alone_is_not_a_change(self):
        """A newer `observed_at` must not be mistaken for a fact change.

        `observed_at` moves on every poll, so comparing it would make every cycle
        differ and therefore every cycle write.
        """
        stored = build_block(_snapshot())
        moved = _snapshot(observed_at="2026-10-06T00:05:00+00:00")

        writes = plan_writes(
            {"dep1": moved}, {"dep1": [_entry(oicm=stored, blocked=False)]}
        )

        assert writes == ()

    def test_status_change_writes_once(self):
        """A Ready to Stopped transition must reach the gateway exactly once."""
        stored = build_block(_snapshot())
        stopped = _snapshot(status="Stopped", serving=False)

        writes = plan_writes(
            {"dep1": stopped}, {"dep1": [_entry(oicm=stored, blocked=False)]}
        )

        assert len(writes) == 1
        assert writes[0].blocked is True
        assert writes[0].block["status"] == "Stopped"

    def test_blocked_alone_is_a_change(self):
        """A routing flip must write even when the rest of the block matches.

        `blocked` is not part of the `oicm` block, so comparing only the block
        would miss a row that is blocked but whose facts have not moved.
        """
        stored = build_block(_snapshot(serving=True))
        serving = _snapshot(serving=True)

        writes = plan_writes(
            {"dep1": serving}, {"dep1": [_entry(oicm=stored, blocked=True)]}
        )

        assert len(writes) == 1
        assert writes[0].blocked is False

    def test_snapshot_without_a_gateway_row_is_skipped(self):
        """An OICM-only deployment has no row to write, and must not crash."""
        writes = plan_writes({"dep1": _snapshot()}, {})

        assert writes == ()


@pytest.mark.asyncio
async def test_persist_writes_only_the_changed_rows():
    """One changed row must not drag its unchanged siblings into a write."""
    stored = build_block(_snapshot(workload_id="dep2"))
    litellm = MagicMock()
    litellm.patch_status = AsyncMock(return_value=True)
    litellm.list_heartbeats = AsyncMock(return_value={})
    litellm.upsert_heartbeat = AsyncMock(return_value=True)
    persister = StatusPersister(litellm)

    await persister.persist(
        {"dep1": _snapshot(workload_id="dep1"), "dep2": _snapshot(workload_id="dep2")},
        {
            "dep1": [_entry("id-1")],
            "dep2": [_entry("id-2", oicm=stored, blocked=False)],
        },
    )

    litellm.patch_status.assert_awaited_once()
    assert litellm.patch_status.await_args.args[0] == "id-1"


@pytest.mark.asyncio
async def test_status_and_blocked_ride_one_patch():
    """A status change must be a single write, not one per changed field."""
    litellm = MagicMock()
    litellm.patch_status = AsyncMock(return_value=True)
    litellm.list_heartbeats = AsyncMock(return_value={})
    litellm.upsert_heartbeat = AsyncMock(return_value=True)
    persister = StatusPersister(litellm)

    await persister.persist(
        {"dep1": _snapshot(status="Stopped", serving=False)},
        {"dep1": [_entry()]},
    )

    litellm.patch_status.assert_awaited_once_with(
        "id-1", True, build_block(_snapshot(status="Stopped", serving=False))
    )


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_one_heartbeat_per_source_not_per_model(self):
        """Liveness is a per-source fact, so it is written once per source.

        Writing it per model would be one full reload per model per tick for a
        timestamp that is identical across a whole source.
        """
        litellm = MagicMock()
        litellm.patch_status = AsyncMock(return_value=True)
        litellm.list_heartbeats = AsyncMock(return_value={})
        litellm.upsert_heartbeat = AsyncMock(return_value=True)
        persister = StatusPersister(litellm)

        await persister.persist(
            {
                "dep1": _snapshot(workload_id="dep1", cluster="alain"),
                "dep2": _snapshot(workload_id="dep2", cluster="alain"),
                "dep3": _snapshot(workload_id="dep3", cluster="abudhabi"),
            },
            {"dep1": [_entry("id-1")], "dep2": [_entry("id-2")], "dep3": [_entry("id-3")]},
        )

        assert litellm.upsert_heartbeat.await_count == 2
        names = {call.args[0]["model_name"] for call in litellm.upsert_heartbeat.await_args_list}
        assert names == {
            f"{HEARTBEAT_NAME_PREFIX}alain",
            f"{HEARTBEAT_NAME_PREFIX}abudhabi",
        }

    @pytest.mark.asyncio
    async def test_heartbeat_reuses_the_existing_row(self):
        """An existing heartbeat must be advanced, not duplicated.

        Without the existing id every tick would POST a new row and the gateway
        would accumulate one heartbeat row per tick.
        """
        litellm = MagicMock()
        litellm.patch_status = AsyncMock(return_value=True)
        litellm.list_heartbeats = AsyncMock(return_value={"oicm-heartbeat-alain": "id-hb"})
        litellm.upsert_heartbeat = AsyncMock(return_value=True)
        persister = StatusPersister(litellm)

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]})

        assert litellm.upsert_heartbeat.await_args.kwargs["existing_id"] == "id-hb"

    @pytest.mark.asyncio
    async def test_heartbeat_is_rate_limited_to_its_own_cadence(self):
        """The heartbeat must not write on every 10s poll."""
        litellm = MagicMock()
        litellm.patch_status = AsyncMock(return_value=True)
        litellm.list_heartbeats = AsyncMock(return_value={})
        litellm.upsert_heartbeat = AsyncMock(return_value=True)
        persister = StatusPersister(litellm, heartbeat_interval=30)

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=0.0)
        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=10.0)

        assert litellm.upsert_heartbeat.await_count == 1

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=31.0)

        assert litellm.upsert_heartbeat.await_count == 2

    @pytest.mark.asyncio
    async def test_no_heartbeat_without_snapshots(self):
        """An empty cycle must not claim the source is alive.

        It must also not consume the heartbeat window: a cycle with nothing to
        report has to leave the next real cycle free to write immediately, or a
        source would look quieter than it is.
        """
        litellm = MagicMock()
        litellm.patch_status = AsyncMock(return_value=True)
        litellm.list_heartbeats = AsyncMock(return_value={})
        litellm.upsert_heartbeat = AsyncMock(return_value=True)
        persister = StatusPersister(litellm, heartbeat_interval=30)

        await persister.persist({}, {}, now=0.0)
        await persister.persist({}, {}, now=1.0)
        litellm.upsert_heartbeat.assert_not_awaited()

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=2.0)

        assert litellm.upsert_heartbeat.await_count == 1

    def test_heartbeat_row_is_not_a_deployment(self):
        """The row must carry no `oicm_uuid`, or rules would treat it as real."""
        row = heartbeat_payload("oicm-heartbeat-alain", "2026-10-06T00:00:00+00:00")

        assert "oicm_uuid" not in row["model_info"]
        assert row["model_info"]["oicm_heartbeat"] == "oicm-heartbeat-alain"
        assert row["model_info"]["checked_at"] == "2026-10-06T00:00:00+00:00"


class TestGatewayUuid:
    def test_submariner_prefix_is_stripped(self):
        """A cross-cluster row must join its own OICM's bare uuid.

        The import namespaces the uuid; the owning OICM returns it bare. Without
        stripping, an Abu Dhabi row could never find its status.
        """
        assert gateway_uuid("submariner:abudhabi:766b1720-aaaa") == "766b1720-aaaa"

    def test_bare_uuid_is_unchanged(self):
        assert gateway_uuid("766b1720-aaaa") == "766b1720-aaaa"

    def test_prefix_without_a_cluster_is_left_intact(self):
        """A malformed value must not be silently mangled into a wrong key."""
        assert gateway_uuid("submariner:766b1720") == "766b1720"
