"""Periodic deployment-status poller.

Polls every configured OICM source's workspace-wide ``deployment_summary`` on a
fixed interval and keeps the latest ``OicmStatusSnapshot`` per workload. One call
per source covers every deployment in that source, so the cadence is independent
of the model count.

OICM refreshes deployment status on a ~5s DB sync plus a ~10s informer reload,
so the default interval matches the source's own update rate rather than
out-polling it.

Sources are polled concurrently. A source that fails keeps its own previous
snapshots and is logged; the other sources' results still land, and a fetch
failure is never mapped to ``offline``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Mapping, Optional, Sequence

from .config import STATUS_SYNC_INTERVAL
from .status import OicmStatusSnapshot, StatusSource, build_snapshot
from .status_persister import StatusPersister

logger = logging.getLogger("oicm-discovery")


class StatusPoller:
    def __init__(
        self,
        sources: Sequence[StatusSource],
        interval: int = STATUS_SYNC_INTERVAL,
        persister: Optional[StatusPersister] = None,
    ):
        self.sources = tuple(sources)
        self.interval = interval
        self.persister = persister
        self._snapshots: Mapping[str, OicmStatusSnapshot] = {}
        self._source_of: Mapping[str, str] = {}
        self._failed_sources: frozenset[str] = frozenset()
        self._running = False

    @property
    def enabled(self) -> bool:
        """Disabled when no source is configured."""
        return bool(self.sources)

    def snapshot(self, workload_id: str) -> Optional[OicmStatusSnapshot]:
        return self._snapshots.get(workload_id)

    @property
    def snapshots(self) -> Mapping[str, OicmStatusSnapshot]:
        return self._snapshots

    @property
    def source_of(self) -> Mapping[str, str]:
        """Which source (cluster) each tracked workload came from."""
        return self._source_of

    @property
    def all_sources_ok(self) -> bool:
        """True when the last cycle heard from every configured source.

        The delete rule depends on this: a source that failed to poll has no
        snapshots through no fault of its deployments, so its absence must not
        be read as deletion.
        """
        return not self._failed_sources

    async def _fetch(self, source: StatusSource) -> tuple[StatusSource, Optional[tuple]]:
        """Fetch one source, returning ``None`` results on failure."""
        try:
            return source, await source.summaries()
        except Exception as e:
            logger.error(
                "Status poll failed for source %s (retaining its last snapshots): %s",
                source.name,
                e,
            )
            return source, None

    async def refresh(self) -> Mapping[str, OicmStatusSnapshot]:
        """Fetch every source once and rebuild the snapshot map.

        Sources are polled concurrently, so the cycle cost is the slowest source
        rather than the sum. A failed source contributes its previous snapshots
        unchanged, so one cluster being unreachable never blanks the other.
        """
        results = await asyncio.gather(*(self._fetch(s) for s in self.sources))

        snapshots: Dict[str, OicmStatusSnapshot] = {}
        source_of: Dict[str, str] = {}
        failed: set[str] = set()
        for source, summaries in results:
            if summaries is None:
                failed.add(source.name)
                for workload_id, snap in self._snapshots.items():
                    if self._source_of.get(workload_id) == source.name:
                        snapshots[workload_id] = snap
                        source_of[workload_id] = source.name
                continue
            for summary in summaries:
                workload_id = summary.deployment_id
                snapshots[workload_id] = build_snapshot(
                    workspace_id=source.workspace_id,
                    source_name=source.name,
                    cluster=source.cluster,
                    summary=summary,
                    previous=self._snapshots.get(workload_id),
                )
                source_of[workload_id] = source.name

        self._log_transitions(snapshots, source_of)
        self._snapshots = snapshots
        self._source_of = source_of
        self._failed_sources = frozenset(failed)
        if self.persister is not None:
            await self.persister.persist_snapshots(snapshots)
        return snapshots

    def _log_transitions(
        self,
        snapshots: Mapping[str, OicmStatusSnapshot],
        source_of: Mapping[str, str],
    ) -> None:
        for workload_id, snap in snapshots.items():
            previous = self._snapshots.get(workload_id)
            if previous is not None and previous.status_changed_at == snap.status_changed_at:
                continue
            logger.info(
                "Status %s [%s]: %s serving=%s replicas=%s/%s",
                workload_id[:8],
                source_of.get(workload_id, "?"),
                snap.source_status.value if snap.source_status else "unknown",
                snap.serving_available,
                snap.available_replicas,
                snap.desired_replicas,
            )

    async def run(self) -> None:
        if not self.enabled:
            logger.warning(
                "Status polling disabled: no OICM sources configured "
                "(set OICM_SOURCES_FILE or mount the sources ConfigMap)"
            )
            return
        self._running = True
        while self._running:
            await self.refresh()
            await asyncio.sleep(self.interval)

    def stop(self) -> None:
        self._running = False

    async def aclose(self) -> None:
        await asyncio.gather(*(s.aclose() for s in self.sources))
