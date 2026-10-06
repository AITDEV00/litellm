"""Tests for the status poller: one call per source, snapshot map, staleness.

Pins behavior, not structure: the poller must fetch every source in one call
each, key snapshots by workload, retain a failed source's previous snapshots
without blanking the others, and stay disabled when no source is configured.
"""

import asyncio
import json
from pathlib import Path

import pytest

from controller.status import OicmDeploymentSummary
from controller.status_poller import StatusPoller

_EVIDENCE = Path(__file__).resolve().parents[2] / "docs" / "oicm-status" / "evidence"


def _summaries():
    raw = json.loads((_EVIDENCE / "deployment-summary.json").read_text())
    return tuple(OicmDeploymentSummary.model_validate(i) for i in raw["items"])


class _FakeSource:
    def __init__(
        self, name="alain", workspace="ws1", cluster=None, summaries=None, error=None
    ):
        self._name = name
        self._workspace = workspace
        self._cluster = cluster or name
        self._summaries = summaries if summaries is not None else _summaries()
        self._error = error
        self.calls = 0

    @property
    def name(self):
        return self._name

    @property
    def cluster(self):
        return self._cluster

    @property
    def workspace_id(self):
        return self._workspace

    async def summaries(self):
        self.calls += 1
        if self._error:
            raise self._error
        return self._summaries

    async def aclose(self):
        return


@pytest.mark.asyncio
async def test_refresh_returns_one_snapshot_per_deployment_from_one_call():
    source = _FakeSource()
    poller = StatusPoller([source])

    snapshots = await poller.refresh()

    assert source.calls == 1
    assert len(snapshots) == len(_summaries())
    assert {s.source_status.value for s in snapshots.values()} == {"Ready", "Deploying"}


@pytest.mark.asyncio
async def test_snapshot_is_keyed_by_workload_id():
    source = _FakeSource()
    poller = StatusPoller([source])
    await poller.refresh()

    for summary in _summaries():
        assert poller.snapshot(summary.deployment_id) is not None
    assert poller.snapshot("does-not-exist") is None


@pytest.mark.asyncio
async def test_status_change_is_tracked_across_refreshes():
    ready = next(s for s in _summaries() if s.status == "Ready")
    source = _FakeSource(summaries=(ready,))
    poller = StatusPoller([source])

    first = (await poller.refresh())[ready.deployment_id]
    assert first.status_changed_at == first.observed_at

    stopped = ready.model_copy(update={"status": "Stopped", "status_detail": ()})
    source._summaries = (stopped,)
    second = (await poller.refresh())[ready.deployment_id]

    assert second.source_status.value == "Stopped"
    assert second.status_changed_at != first.status_changed_at


@pytest.mark.asyncio
async def test_failed_poll_retains_previous_snapshots():
    source = _FakeSource()
    poller = StatusPoller([source])
    await poller.refresh()
    before = {k: v.serving_available for k, v in poller.snapshots.items()}

    source._error = RuntimeError("oicm down")
    await poller.refresh()

    assert {k: v.serving_available for k, v in poller.snapshots.items()} == before


@pytest.mark.asyncio
async def test_one_failing_source_does_not_blank_the_others():
    """A cluster being unreachable must not drop the other cluster's models.

    This is the case that matters once two OICMs are polled: an Abu Dhabi outage
    must not make Al Ain's deployments look deleted.
    """
    good = _FakeSource(name="alain")
    bad = _FakeSource(name="abudhabi", summaries=())
    poller = StatusPoller([good, bad])
    await poller.refresh()
    before = {k: v.serving_available for k, v in poller.snapshots.items()}

    bad._error = RuntimeError("ad oicm down")
    await poller.refresh()

    # The healthy source keeps its deployments, with their status facts intact.
    # observed_at advances on a successful poll, so only the facts are compared.
    assert {k: v.serving_available for k, v in poller.snapshots.items()} == before
    assert set(poller.source_of.values()) == {"alain"}


@pytest.mark.asyncio
async def test_failed_source_retains_its_own_snapshots():
    """The failing source's own last snapshots survive its failure."""
    ad = _FakeSource(name="abudhabi", summaries=_summaries())
    poller = StatusPoller([ad])
    await poller.refresh()
    before = {k: v.serving_available for k, v in poller.snapshots.items()}
    assert before

    ad._error = RuntimeError("ad oicm down")
    await poller.refresh()

    assert {k: v.serving_available for k, v in poller.snapshots.items()} == before
    assert set(poller.source_of.values()) == {"abudhabi"}


@pytest.mark.asyncio
async def test_snapshots_record_which_source_produced_them():
    alain = _FakeSource(name="alain", summaries=_summaries())
    ad = _FakeSource(name="abudhabi", summaries=())
    poller = StatusPoller([alain, ad])

    await poller.refresh()

    assert set(poller.source_of.values()) == {"alain"}
    # The cluster rides on the snapshot itself, which is what lets a consumer
    # look up the heartbeat that says whether the status is still fresh.
    assert {s.cluster for s in poller.snapshots.values()} == {"alain"}


@pytest.mark.asyncio
async def test_all_sources_ok_is_false_when_any_source_fails():
    """The delete rule reads this, so a failed source must be reported.

    A source that failed to poll has no snapshots through no fault of its
    deployments, and `all_sources_ok` is what stops that absence being read as
    deletion.
    """
    good = _FakeSource(name="alain")
    bad = _FakeSource(name="abudhabi", summaries=())
    poller = StatusPoller([good, bad])

    await poller.refresh()
    assert poller.all_sources_ok is True

    bad._error = RuntimeError("ad oicm down")
    await poller.refresh()
    assert poller.all_sources_ok is False

    bad._error = None
    await poller.refresh()
    assert poller.all_sources_ok is True


@pytest.mark.asyncio
async def test_sources_are_polled_concurrently():
    """One slow source must not serialize the cycle behind the other."""
    order = []

    class _Slow(_FakeSource):
        async def summaries(self):
            order.append("slow-start")
            await asyncio.sleep(0.05)
            order.append("slow-end")
            return await super().summaries()

    class _Fast(_FakeSource):
        async def summaries(self):
            order.append("fast")
            return await super().summaries()

    poller = StatusPoller([_Slow(name="slow"), _Fast(name="fast")])
    await poller.refresh()

    # The fast source completes while the slow one is still sleeping.
    assert order.index("fast") < order.index("slow-end")


@pytest.mark.asyncio
async def test_disabled_without_sources():
    poller = StatusPoller([])
    assert poller.enabled is False
    # run() returns immediately rather than polling forever.
    await asyncio.wait_for(poller.run(), timeout=1)


@pytest.mark.asyncio
async def test_run_polls_then_stops():
    source = _FakeSource()
    poller = StatusPoller([source], interval=0)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await asyncio.wait_for(task, timeout=1)
    assert source.calls >= 2


@pytest.mark.asyncio
async def test_snapshot_records_source_name_and_cluster_separately():
    """A source named for its role still records the cluster it serves.

    The two are distinct fields: the cluster answers "Abu Dhabi or Al Ain" and
    is what a consumer keys the heartbeat lookup on, while the source name says
    which configured OICM reported the status.
    """
    source = _FakeSource(name="primary-oicm", cluster="alain")
    poller = StatusPoller([source])

    snapshots = await poller.refresh()

    snap = next(iter(snapshots.values()))
    assert snap.source_name == "primary-oicm"
    assert snap.cluster == "alain"
