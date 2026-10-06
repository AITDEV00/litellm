"""Periodic deployment-status poller.

Polls the OICM workspace-wide ``deployment_summary`` on a fixed interval and
keeps the latest ``OicmStatusSnapshot`` per workload. One call covers every
deployment, so the cadence is independent of the model count.

OICM refreshes deployment status on a ~5s DB sync plus a ~10s informer reload,
so the default interval matches the source's own update rate rather than
out-polling it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Mapping, Optional

from .config import OICM_WORKSPACE_ID, STATUS_SYNC_INTERVAL
from .status import OicmStatusSnapshot, OicmStatusSource, StatusSource, build_snapshot

logger = logging.getLogger("oicm-discovery")


class StatusPoller:
    def __init__(
        self,
        workspace_id: str = OICM_WORKSPACE_ID,
        interval: int = STATUS_SYNC_INTERVAL,
        source: Optional[StatusSource] = None,
    ):
        self.workspace_id = workspace_id
        self.interval = interval
        self.source = source or OicmStatusSource()
        self._snapshots: Mapping[str, OicmStatusSnapshot] = {}
        self._running = False

    @property
    def enabled(self) -> bool:
        """Disabled when no workspace is configured (no workspace-list endpoint)."""
        return bool(self.workspace_id)

    def snapshot(self, workload_id: str) -> Optional[OicmStatusSnapshot]:
        return self._snapshots.get(workload_id)

    @property
    def snapshots(self) -> Mapping[str, OicmStatusSnapshot]:
        return self._snapshots

    async def refresh(self) -> Mapping[str, OicmStatusSnapshot]:
        """Fetch once and replace the snapshot map.

        Returns the new map so callers (and tests) can inspect the result
        without reaching into private state.
        """
        summaries = await self.source.summaries(self.workspace_id)
        snapshots = {
            s.deployment_id: build_snapshot(
                workspace_id=self.workspace_id,
                summary=s,
                previous=self._snapshots.get(s.deployment_id),
            )
            for s in summaries
        }
        self._log_transitions(snapshots)
        self._snapshots = snapshots
        return snapshots

    def _log_transitions(self, snapshots: Mapping[str, OicmStatusSnapshot]) -> None:
        for workload_id, snap in snapshots.items():
            previous = self._snapshots.get(workload_id)
            if previous is not None and previous.status_changed_at == snap.status_changed_at:
                continue
            logger.info(
                "Status %s: %s serving=%s replicas=%s/%s",
                workload_id[:8],
                snap.source_status.value if snap.source_status else "unknown",
                snap.serving_available,
                snap.available_replicas,
                snap.desired_replicas,
            )

    async def run(self) -> None:
        if not self.enabled:
            logger.warning(
                "Status polling disabled: set OICM_WORKSPACE_ID to enable it"
            )
            return
        self._running = True
        while self._running:
            try:
                await self.refresh()
            except Exception as e:
                logger.error("Status poll failed (retaining last snapshots): %s", e)
            await asyncio.sleep(self.interval)

    def stop(self) -> None:
        self._running = False

    async def aclose(self) -> None:
        await self.source.aclose()
