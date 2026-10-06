"""Tests for persisting OICM status onto the LiteLLM rows.

Pins behavior, not structure: a status change must reach the gateway exactly
once, an unchanged cycle must write nothing, a lone `observed_at` must not be
mistaken for a change, the block must be complete, liveness must be written
per source rather than per model, and health reports must follow the native
loop's on-change-plus-hourly-refresh rule.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from controller.litellm_client import gateway_uuid
from controller.status.snapshot import DeploymentStatus, OicmStatusSnapshot
from controller.status_persister import (
    HEALTH_REFRESH_SECONDS,
    StatusPersister,
    build_block,
    plan_writes,
)

def _litellm_mock():
    litellm = MagicMock()
    litellm.patch_status = AsyncMock(return_value=True)
    litellm.report_status = AsyncMock(return_value=True)
    litellm.report_heartbeats = AsyncMock(
        return_value={"alain": True, "abudhabi": True}
    )
    return litellm

def _snapshot(
    workload_id="dep1",
    status="Ready",
    serving=True,
    cluster="alain",
    source_name="alain",
    observed_at="2026-10-06T00:00:00+00:00",
    status_changed_at="2026-10-06T00:00:00+00:00",
    error_msg=None,
    desired=1,
    available=1,
):
    return OicmStatusSnapshot(
        workspace_id="ws1",
        workload_id=workload_id,
        source_name=source_name,
        cluster=cluster,
        source_status=DeploymentStatus(status) if status else None,
        desired_replicas=desired,
        available_replicas=available,
        unavailable_replicas=None,
        serving_available=serving,
        error_msg=error_msg,
        status_changed_at=status_changed_at,
        observed_at=observed_at,
    )


def _entry(model_id="id-1", oicm=None, blocked=False, cluster="alain", source_name="alain"):
    info = {
        "id": model_id,
        "oicm_uuid": "dep1",
        "oicm_cluster": cluster,
        "oicm_source_name": source_name,
        "blocked": blocked,
    }
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
        routing reads, so a second copy could only drift. The cluster is also
        absent, because it lives in the sibling `oicm_cluster` key rather than in
        the status block.
        """
        block = build_block(_snapshot())

        assert set(block) == {
            "v",
            "status",
            "serving_available",
            "gateway_uuid",
            "replicas",
            "status_changed_at",
            "observed_at",
            "error_msg",
        }
        assert block["v"] == 1
        assert block["replicas"] == {"desired": 1, "available": 1}
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

    def test_missing_cluster_is_a_change(self):
        """A row that predates `oicm_cluster` must gain it on the next write.

        The cluster is what answers "Abu Dhabi or Al Ain" for a model, so a row
        without it has to be filled in rather than treated as unchanged.
        """
        stored = build_block(_snapshot())
        entry = _entry(oicm=stored, blocked=False)
        entry["model_info"].pop("oicm_cluster")

        writes = plan_writes({"dep1": _snapshot()}, {"dep1": [entry]})

        assert len(writes) == 1
        assert writes[0].cluster == "alain"

    def test_missing_source_name_is_a_change(self):
        """A row that predates `oicm_source_name` must gain it too.

        The source name is what identifies the configured OICM that reported the
        status, which is the lookup a consumer uses for freshness.
        """
        stored = build_block(_snapshot())
        entry = _entry(oicm=stored, blocked=False)
        entry["model_info"].pop("oicm_source_name")

        writes = plan_writes({"dep1": _snapshot()}, {"dep1": [entry]})

        assert len(writes) == 1
        assert writes[0].source_name == "alain"

    def test_snapshot_without_a_gateway_row_is_skipped(self):
        """An OICM-only deployment has no row to write, and must not crash."""
        writes = plan_writes({"dep1": _snapshot()}, {})

        assert writes == ()


@pytest.mark.asyncio
async def test_persist_writes_only_the_changed_rows():
    """One changed row must not drag its unchanged siblings into a write."""
    stored = build_block(_snapshot(workload_id="dep2"))
    litellm = _litellm_mock()
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
    litellm = _litellm_mock()
    persister = StatusPersister(litellm)

    await persister.persist(
        {"dep1": _snapshot(status="Stopped", serving=False)},
        {"dep1": [_entry()]},
    )

    litellm.patch_status.assert_awaited_once_with(
        "id-1",
        True,
        "alain",
        "alain",
        build_block(_snapshot(status="Stopped", serving=False)),
    )


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_one_heartbeat_per_source_not_per_model(self):
        """Liveness is a per-source fact, so it is reported once per source.

        Writing it per model would multiply rows for a fact that is identical
        across a whole source.
        """
        litellm = _litellm_mock()
        persister = StatusPersister(litellm)

        await persister.persist(
            {
                "dep1": _snapshot(workload_id="dep1", cluster="alain"),
                "dep2": _snapshot(workload_id="dep2", cluster="alain"),
                "dep3": _snapshot(workload_id="dep3", cluster="abudhabi"),
            },
            {"dep1": [_entry("id-1")], "dep2": [_entry("id-2")], "dep3": [_entry("id-3")]},
        )

        litellm.report_heartbeats.assert_awaited_once()
        assert litellm.report_heartbeats.await_args.args[0] == ["abudhabi", "alain"]

    @pytest.mark.asyncio
    async def test_heartbeat_is_rate_limited_to_its_own_cadence(self):
        """The heartbeat must not write on every 10s poll."""
        litellm = _litellm_mock()
        persister = StatusPersister(litellm, heartbeat_interval=30)

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=0.0)
        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=10.0)

        assert litellm.report_heartbeats.await_count == 1

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=31.0)

        assert litellm.report_heartbeats.await_count == 2

    @pytest.mark.asyncio
    async def test_no_heartbeat_without_snapshots(self):
        """An empty cycle must not claim the source is alive.

        It must also not consume the heartbeat window: a cycle with nothing to
        report has to leave the next real cycle free to write immediately, or a
        source would look quieter than it is.
        """
        litellm = _litellm_mock()
        persister = StatusPersister(litellm, heartbeat_interval=30)

        await persister.persist({}, {}, now=0.0)
        await persister.persist({}, {}, now=1.0)
        litellm.report_heartbeats.assert_not_awaited()

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=2.0)

        assert litellm.report_heartbeats.await_count == 1

    @pytest.mark.asyncio
    async def test_failed_heartbeat_is_not_recorded_as_alive(self):
        """A source the gateway refused must not appear in `checked_at`.

        `checked_at` feeds the controller's own /status, so a failed write
        recorded as success would let the controller claim liveness it does not
        have.
        """
        litellm = _litellm_mock()
        litellm.report_heartbeats = AsyncMock(return_value={})
        persister = StatusPersister(litellm)

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]})

        assert persister.checked_at == {}


class TestHealthReports:
    """The per-model rows the gateway's native /health/latest reads."""

    @pytest.mark.asyncio
    async def test_first_cycle_reports_every_registered_model(self):
        """A model with no health row yet must get one, or /health/latest stays empty."""
        litellm = _litellm_mock()
        persister = StatusPersister(litellm)

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]})

        litellm.report_status.assert_awaited_once()
        report = litellm.report_status.await_args.args[0][0]
        assert report["litellm_model_id"] == "id-1"
        assert report["model_name"] == "hosted_vllm/test-model"
        assert report["healthy"] is True

    @pytest.mark.asyncio
    async def test_steady_state_reports_nothing(self):
        """A stable model must not gain a health row on every 10s poll.

        The hourly refresh rule is what keeps the volume bounded: identical
        cycles write nothing, exactly like the PATCH guard.
        """
        litellm = _litellm_mock()
        persister = StatusPersister(litellm)

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=0.0)
        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=10.0)

        assert litellm.report_status.await_count == 1

    @pytest.mark.asyncio
    async def test_stable_model_is_refreshed_after_an_hour(self):
        """A long-stable model must still get a fresh row eventually.

        Without the refresh, the Admin UI's "last check" column would read
        "3 days ago" for a perfectly healthy model, which looks like an outage.
        This mirrors the native loop's one-hour threshold exactly, so if that
        loop is ever enabled both writers behave identically.
        """
        litellm = _litellm_mock()
        persister = StatusPersister(litellm)

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=0.0)
        await persister.persist(
            {"dep1": _snapshot()},
            {"dep1": [_entry()]},
            now=HEALTH_REFRESH_SECONDS + 1,
        )

        assert litellm.report_status.await_count == 2

    @pytest.mark.asyncio
    async def test_serving_flip_is_reported_even_without_the_patch(self):
        """`healthy` must follow `serving_available`, the same verdict a probe would return.

        A `Ready` deployment whose pods are down is unhealthy here; writing
        `healthy` would publish a model that fails every request.
        """
        litellm = _litellm_mock()
        persister = StatusPersister(litellm)

        await persister.persist(
            {"dep1": _snapshot(serving=False)}, {"dep1": [_entry()]}, now=0.0
        )

        report = litellm.report_status.await_args.args[0][0]
        assert report["healthy"] is False
        assert report["error_message"] is None

    @pytest.mark.asyncio
    async def test_report_carries_the_block_and_cluster(self):
        """The gateway row must carry the OICM truth inside `details`.

        The health column can only say healthy/unhealthy; the lifecycle word,
        replicas and cluster are what make the row worth reading, and they ride
        in `details` so `/health/latest` exposes them verbatim.
        """
        litellm = _litellm_mock()
        persister = StatusPersister(litellm)

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]})

        report = litellm.report_status.await_args.args[0][0]
        assert report["details"]["status"] == "Ready"
        assert report["details"]["cluster"] == "alain"
        assert report["details"]["serving_available"] is True

    @pytest.mark.asyncio
    async def test_snapshot_without_a_gateway_row_is_not_reported(self):
        """An OICM-only deployment has no LiteLLM row to attach a health report to."""
        litellm = _litellm_mock()
        persister = StatusPersister(litellm)

        await persister.persist({"dep1": _snapshot()}, {})

        litellm.report_status.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_report_retries_next_cycle(self):
        """A refused batch must not be recorded as reported.

        The refresh bookkeeping only advances on success, so a gateway hiccup
        costs one extra attempt rather than an hour of missing health rows.
        """
        litellm = _litellm_mock()
        litellm.report_status = AsyncMock(side_effect=[False, True])
        persister = StatusPersister(litellm)

        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=0.0)
        await persister.persist({"dep1": _snapshot()}, {"dep1": [_entry()]}, now=10.0)

        assert litellm.report_status.await_count == 2


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


class TestCrossClusterStatus:
    """The Abu Dhabi import must get its status from Abu Dhabi's own OICM.

    This is the whole point of stripping the prefix: the gateway row is keyed on
    `submariner:abudhabi:<uuid>` while Abu Dhabi's OICM reports the bare uuid, so
    before the strip the row could never find its snapshot and a stopped AD
    deployment stayed routable.
    """

    def test_import_is_paused_when_its_own_oicm_reports_not_serving(self):
        imported = _snapshot(
            workload_id="766b1720",
            status="Stopped",
            serving=False,
            cluster="abudhabi",
        )
        entry = _entry("id-ad", blocked=False, cluster="abudhabi")
        entry["model_info"]["oicm_uuid"] = "submariner:abudhabi:766b1720"

        writes = plan_writes({"766b1720": imported}, {"766b1720": [entry]})

        assert len(writes) == 1
        assert writes[0].litellm_model_id == "id-ad"
        assert writes[0].blocked is True
        assert writes[0].cluster == "abudhabi"

    def test_import_is_resumed_when_its_own_oicm_reports_serving(self):
        stored = build_block(_snapshot(workload_id="766b1720", serving=True))
        imported = _snapshot(workload_id="766b1720", serving=True, cluster="abudhabi")
        entry = _entry("id-ad", oicm=stored, blocked=True, cluster="abudhabi")
        entry["model_info"]["oicm_uuid"] = "submariner:abudhabi:766b1720"

        writes = plan_writes({"766b1720": imported}, {"766b1720": [entry]})

        assert len(writes) == 1
        assert writes[0].blocked is False
