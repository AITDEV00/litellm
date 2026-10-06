"""Tests for the status poller: one workspace call, snapshot map, staleness.

Pins behavior, not structure: the poller must fetch every deployment in one
call, key snapshots by workload, retain the previous snapshot on failure, and
stay disabled when no workspace is configured.
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
    def __init__(self, summaries=None, error=None):
        self._summaries = summaries if summaries is not None else _summaries()
        self._error = error
        self.calls = 0

    async def summaries(self, workspace_id):
        self.calls += 1
        if self._error:
            raise self._error
        return self._summaries

    async def aclose(self):
        return


@pytest.mark.asyncio
async def test_refresh_returns_one_snapshot_per_deployment_from_one_call():
    source = _FakeSource()
    poller = StatusPoller(workspace_id="ws1", source=source)

    snapshots = await poller.refresh()

    assert source.calls == 1
    assert len(snapshots) == len(_summaries())
    assert {s.source_status.value for s in snapshots.values()} == {"Ready", "Deploying"}


@pytest.mark.asyncio
async def test_snapshot_is_keyed_by_workload_id():
    source = _FakeSource()
    poller = StatusPoller(workspace_id="ws1", source=source)
    await poller.refresh()

    for summary in _summaries():
        assert poller.snapshot(summary.deployment_id) is not None
    assert poller.snapshot("does-not-exist") is None


@pytest.mark.asyncio
async def test_status_change_is_tracked_across_refreshes():
    ready = next(s for s in _summaries() if s.status == "Ready")
    source = _FakeSource(summaries=(ready,))
    poller = StatusPoller(workspace_id="ws1", source=source)

    first = (await poller.refresh())[ready.deployment_id]
    assert first.status_changed_at == first.observed_at

    stopped = ready.model_copy(update={"status": "Stopped", "status_detail": ()})
    source._summaries = (stopped,)
    second = (await poller.refresh())[ready.deployment_id]

    assert second.previous_source_status.value == "Ready"
    assert second.source_status.value == "Stopped"
    assert second.status_changed_at != first.status_changed_at


@pytest.mark.asyncio
async def test_failed_poll_retains_previous_snapshots():
    source = _FakeSource()
    poller = StatusPoller(workspace_id="ws1", source=source)
    await poller.refresh()
    before = dict(poller.snapshots)

    source._error = RuntimeError("oicm down")
    with pytest.raises(RuntimeError):
        await poller.refresh()

    assert poller.snapshots == before


@pytest.mark.asyncio
async def test_disabled_without_workspace():
    poller = StatusPoller(workspace_id="", source=_FakeSource())
    assert poller.enabled is False
    # run() returns immediately rather than polling forever.
    await asyncio.wait_for(poller.run(), timeout=1)


@pytest.mark.asyncio
async def test_run_polls_then_stops():
    source = _FakeSource()
    poller = StatusPoller(workspace_id="ws1", interval=0, source=source)
    task = asyncio.create_task(poller.run())
    await asyncio.sleep(0.05)
    poller.stop()
    await asyncio.wait_for(task, timeout=1)
    assert source.calls >= 2
