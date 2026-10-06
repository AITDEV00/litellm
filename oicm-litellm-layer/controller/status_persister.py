"""Persist OICM status onto the LiteLLM model rows.

Runs after every ``StatusPoller.refresh()`` and writes two things:

- Per model: the ``model_info.oicm`` block and the routing flag ``blocked``, in
  one PATCH. Written only when a fact differs, so a steady-state cluster issues
  zero status writes.
- Per source: a liveness row carrying ``checked_at``, on its own slower cadence.
  Liveness belongs to the source rather than the model, so this is one write per
  source instead of one per model, and a consumer reads it to decide whether a
  persisted status is still fresh.

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
from datetime import datetime, timezone

from .config import HEARTBEAT_INTERVAL
from .litellm_client import LiteLLMClient, heartbeat_payload
from .status.snapshot import OicmStatusSnapshot

logger = logging.getLogger("oicm-discovery")

# The block shape version. The block is replaced wholesale, so a future change
# needs a discriminator to tell an old row from a new one.
BLOCK_VERSION = 1

# Heartbeat rows are named so they sort together and read as controller-owned.
HEARTBEAT_NAME_PREFIX = "oicm-heartbeat-"

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
    ):
        self.litellm = litellm
        self.heartbeat_interval = heartbeat_interval
        self._last_heartbeat: float | None = None
        # Last confirmed `checked_at` per source, so the health endpoint can show
        # liveness without a gateway read.
        self.checked_at: Mapping[str, str] = {}

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

        await self._maybe_heartbeat(snapshots, now=now)
        return len(writes)

    async def _maybe_heartbeat(
        self,
        snapshots: Mapping[str, OicmStatusSnapshot],
        *,
        now: float | None = None,
    ) -> None:
        """Advance the per-source liveness rows, at most once per heartbeat.

        Skipped entirely when there are no snapshots: a source that is down
        should not look alive, and a cycle with nothing to report is exactly when
        a heartbeat would be a lie.
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
        checked_at = datetime.now(timezone.utc).isoformat()
        existing = await self.litellm.list_heartbeats()
        results = await asyncio.gather(
            *(
                self.litellm.upsert_heartbeat(
                    heartbeat_payload(
                        f"{HEARTBEAT_NAME_PREFIX}{cluster}", checked_at, cluster
                    ),
                    existing_id=existing.get(f"{HEARTBEAT_NAME_PREFIX}{cluster}"),
                )
                for cluster in clusters
            )
        )
        self.checked_at = {
            cluster: checked_at
            for cluster, ok in zip(clusters, results, strict=True)
            if ok
        }


def _monotonic() -> float:
    return asyncio.get_event_loop().time()
