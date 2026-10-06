"""Persist OICM status onto the LiteLLM model rows.

Runs after every ``StatusPoller.refresh()`` and writes three things:

- Per model: the ``model_info.oicm`` block and the routing flag ``blocked``, in
  one PATCH. Written only when a fact differs, so a steady-state cluster issues
  zero status writes.
- Per model: a row in the gateway's native health table, so `/health/latest`
  and the Admin UI health column carry OICM truth. Written on the native loop's
  rule: on change, else when the last row is older than an hour, so a stable
  model does not read as "last checked 3 days ago".
- Per source: a liveness row in the same table, on its own slower cadence.
  Liveness belongs to the source rather than the model, so this is one write
  per source instead of one per model, and a consumer reads it to decide whether
  a persisted status is still fresh.

The block deliberately does not include ``api_base``: ``litellm_params.api_base``
already survives independently and is what routing reads, so a second copy could
only drift. ``observed_at`` is deliberately excluded from the change comparison,
because including it would make every cycle differ and therefore every cycle
write.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from .config import HEARTBEAT_INTERVAL
from .litellm_client import LiteLLMClient
from .status.snapshot import OicmStatusSnapshot

logger = logging.getLogger("oicm-discovery")

# The block shape version. The block is replaced wholesale, so a future change
# needs a discriminator to tell an old row from a new one.
BLOCK_VERSION = 1

# Mirrors the native background loop's periodic-refresh threshold
# (`_should_persist_health_check_result`): a stable model still gets a fresh
# health row once an hour so the Admin UI's "last check" column stays honest.
HEALTH_REFRESH_SECONDS = 3600

# The facts a change is judged on. `observed_at` is absent on purpose: it moves
# every cycle, so comparing it would defeat the write guard entirely.
_COMPARED_FACTS = (
    "v",
    "status",
    "serving_available",
    "gateway_uuid",
    "replicas",
    "status_changed_at",
    "error_msg",
)


def build_block(snapshot: OicmStatusSnapshot) -> dict:
    """The complete `model_info.oicm` object for one snapshot.

    Complete, not partial: LiteLLM merges `model_info` shallowly, so a partial
    block would replace the `oicm` object and drop whatever it omitted.

    The cluster is not in here. It is a sibling key (`oicm_cluster`) rather than
    a status fact, because it answers "which cluster is this deployment in" at
    any time, not only when a status was observed.
    """
    return {
        "v": BLOCK_VERSION,
        "status": snapshot.source_status.value if snapshot.source_status else None,
        "serving_available": snapshot.serving_available,
        "gateway_uuid": snapshot.workload_id,
        "replicas": {
            "desired": snapshot.desired_replicas,
            "available": snapshot.available_replicas,
        },
        "status_changed_at": snapshot.status_changed_at,
        "observed_at": snapshot.observed_at,
        "error_msg": snapshot.error_msg,
    }


def _facts(block: Mapping[str, object]) -> dict:
    return {key: block.get(key) for key in _COMPARED_FACTS}


def _block_changed(stored: dict | None, fresh: dict) -> bool:
    """True when any compared fact differs from what the row already carries.

    An absent block counts as changed, which is what writes a row the first time.
    """
    if not isinstance(stored, dict):
        return True
    return _facts(stored) != _facts(fresh)


@dataclass(frozen=True, slots=True)
class StatusWrite:
    """One model's pending write: which row, and what to put on it."""

    litellm_model_id: str
    blocked: bool
    cluster: str
    source_name: str
    block: dict


def plan_writes(
    snapshots: Mapping[str, OicmStatusSnapshot],
    litellm_by_uuid: Mapping[str, Sequence[dict]],
) -> tuple[StatusWrite, ...]:
    """Decide which rows need a status write this cycle.

    A snapshot whose uuid has no gateway row is skipped: the reconciler has not
    registered it (an OICM-only deployment), or the row is a Submariner import
    that the import source owns.

    OICM `serving_available` is the sole owner of `blocked`. The k8s watch used
    to reconcile the same column from `ready_replicas`, which is a second probe
    of the same fact; only one can be authoritative, and this is it.
    """
    writes: list[StatusWrite] = []
    for workload_id, snapshot in snapshots.items():
        entries = litellm_by_uuid.get(workload_id)
        if not entries:
            continue
        block = build_block(snapshot)
        blocked = not snapshot.serving_available
        for entry in entries:
            model_id = entry.get("model_id")
            if not model_id:
                continue
            info = entry.get("model_info") or {}
            stored = info.get("oicm")
            if (
                not _block_changed(stored, block)
                and info.get("blocked", False) is blocked
                and info.get("oicm_cluster") == snapshot.cluster
                and info.get("oicm_source_name") == snapshot.source_name
            ):
                continue
            writes.append(
                StatusWrite(
                    litellm_model_id=model_id,
                    blocked=blocked,
                    cluster=snapshot.cluster,
                    source_name=snapshot.source_name,
                    block=block,
                )
            )
    return tuple(writes)


class StatusPersister:
    """Writes polled status to LiteLLM. Owns the 10s clock's write half."""

    def __init__(
        self,
        litellm: LiteLLMClient,
        heartbeat_interval: int = HEARTBEAT_INTERVAL,
        health_refresh_seconds: int = HEALTH_REFRESH_SECONDS,
    ):
        self.litellm = litellm
        self.heartbeat_interval = heartbeat_interval
        self.health_refresh_seconds = health_refresh_seconds
        self._last_heartbeat: float | None = None
        # Last per-model health write, so the hourly refresh rule can decide
        # without a gateway read. Keys are LiteLLM model ids.
        self._last_health_at: dict[str, float] = {}
        self.checked_at: Mapping[str, bool] = {}

    async def persist_snapshots(
        self,
        snapshots: Mapping[str, OicmStatusSnapshot],
        *,
        now: float | None = None,
    ) -> int:
        """Read the gateway rows and persist status for this cycle.

        The entry point the poller calls. The row read is the grouping the
        reconciler already uses, so this adds no new gateway call shape, and a
        read never triggers a reload.
        """
        if not snapshots:
            return 0
        litellm_by_uuid = await self.litellm.list_all_models_by_key()
        return await self.persist(snapshots, litellm_by_uuid, now=now)

    async def persist(
        self,
        snapshots: Mapping[str, OicmStatusSnapshot],
        litellm_by_uuid: Mapping[str, Sequence[dict]],
        *,
        now: float | None = None,
    ) -> int:
        """Write status for the snapshots whose facts changed. Returns the count.

        `litellm_by_uuid` is the grouping the reconciler already fetched, so this
        costs no extra gateway read. The heartbeat is a separate, slower write
        and is not part of the returned count.
        """
        current = now if now is not None else _monotonic()
        writes = plan_writes(snapshots, litellm_by_uuid)
        if writes:
            results = await asyncio.gather(
                *(
                    self.litellm.patch_status(
                        w.litellm_model_id,
                        w.blocked,
                        w.cluster,
                        w.source_name,
                        w.block,
                    )
                    for w in writes
                )
            )
            written = sum(1 for r in results if r)
            logger.info("Persisted status on %d/%d models", written, len(writes))

        await self._maybe_report_health(snapshots, litellm_by_uuid, current)
        await self._maybe_heartbeat(snapshots, now=current)
        return len(writes)

    async def _maybe_report_health(
        self,
        snapshots: Mapping[str, OicmStatusSnapshot],
        litellm_by_uuid: Mapping[str, Sequence[dict]],
        now: float,
    ) -> None:
        """Send one health-report batch for the rows that need a health row.

        The native loop's rule, applied to OICM truth: a row just PATCHed this
        cycle is reported (that is the change), and a row whose last report is
        older than an hour is refreshed, so a stable model still shows a recent
        "last check" in the Admin UI instead of reading as long-unchecked.
        """
        reports: list[dict] = []
        for workload_id, snapshot in snapshots.items():
            entries = litellm_by_uuid.get(workload_id)
            if not entries:
                continue
            block = build_block(snapshot)
            for entry in entries:
                model_id = entry.get("model_id")
                if not model_id:
                    continue
                last = self._last_health_at.get(model_id)
                if last is not None and (now - last) < self.health_refresh_seconds:
                    continue
                reports.append(
                    {
                        "model_name": entry.get("model_name") or model_id,
                        "litellm_model_id": model_id,
                        "healthy": snapshot.serving_available,
                        "error_message": snapshot.error_msg,
                        "details": {**block, "cluster": snapshot.cluster},
                    }
                )
        if not reports:
            return
        if await self.litellm.report_status(reports):
            for r in reports:
                self._last_health_at[r["litellm_model_id"]] = now
            logger.debug("Reported %d health rows", len(reports))

    async def _maybe_heartbeat(
        self,
        snapshots: Mapping[str, OicmStatusSnapshot],
        *,
        now: float | None = None,
    ) -> None:
        """Record per-source liveness, at most once per heartbeat.

        Skipped entirely when there are no snapshots: a source that is down
        should not look alive, and a cycle with nothing to report is exactly when
        a heartbeat would be a lie.

        The server stamps `checked_at`, so the timestamp is the gateway's own
        "when did I last hear from the controller" and cannot drift from what a
        consumer reads back.
        """
        if not snapshots:
            return
        current = now if now is not None else _monotonic()
        if (
            self._last_heartbeat is not None
            and current - self._last_heartbeat < self.heartbeat_interval
        ):
            return
        self._last_heartbeat = current

        clusters = sorted({snap.cluster for snap in snapshots.values()})
        results = await self.litellm.report_heartbeats(clusters)
        if results:
            self.checked_at = MappingProxyType(dict(results))


def _monotonic() -> float:
    return asyncio.get_event_loop().time()
