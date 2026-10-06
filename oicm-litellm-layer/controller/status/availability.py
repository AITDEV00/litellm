"""Serving availability derived from a workload run's ``status_detail``.

Mirrors OICM ``DeploymentService.__is_deployment_available`` in intent:
multi-node = any LeaderWorkerSet entry with ``metadata.available``; single-node
= any Pod with a node assigned and ``metadata.ready``.

Two fields OICM leaves null in one version or the other are deliberately treated
as absent-safe rather than required:

- ``apiVersion``: OICM's own check requires ``"v1"``, but the API returns null
  for every ``status_detail`` entry, so requiring it makes OICM's availability
  (and hence ``/health.is_ready``) report False for every deployment.
- ``metadata``: OICM ``1.7.1`` (Abu Dhabi) does not populate it at all. Every
  entry there carries only ``kind``, ``name``, ``node``, ``status``, and
  ``status_msg``, so requiring ``metadata.ready`` makes a genuinely serving
  deployment read as not serving.

Where ``metadata`` is absent the entry's own ``status`` carries the same fact and
is populated by both versions, so it is the fallback. The ``Deployment`` entry is
deliberately not consulted: it can report available while a rollout is still in
progress, so the Pod and LeaderWorkerSet entries remain the serving signal.

The match is over the typed discriminated-union variants, so a new OICM ``kind``
falls through to ``False`` instead of being silently misread.
"""

from __future__ import annotations

from typing import Optional

from .wire import (
    DeploymentStatusDetail,
    LeaderWorkerSetStatusDetail,
    PodStatusDetail,
    StatusDetail,
)

# Entry status strings that mean "serving", consulted only when the entry
# carries no metadata. Compared case-insensitively.
_SERVING_STATUSES = frozenset({"ready", "running", "available"})


def _status_says_serving(status: Optional[str]) -> bool:
    return isinstance(status, str) and status.strip().lower() in _SERVING_STATUSES


def _is_ready(entry: StatusDetail) -> bool:
    match entry:
        case PodStatusDetail(node=node, status=status, metadata=meta):
            # A pod with no node is not scheduled, so it cannot serve.
            if not node:
                return False
            if meta is not None:
                return bool(meta.ready)
            return _status_says_serving(status)
        case LeaderWorkerSetStatusDetail(status=status, metadata=meta):
            if meta is not None:
                return bool(meta.available)
            return _status_says_serving(status)
        case DeploymentStatusDetail():
            # The Deployment entry is not a serving signal on its own: it can
            # report available while the rollout is still in progress, which is
            # why the Pod and LeaderWorkerSet entries are the ones consulted.
            return False


def is_deployment_available(status_detail: tuple[StatusDetail, ...]) -> bool:
    """True when any status_detail entry reports a serving-ready object.

    Works for either topology without a topology flag: a Pod entry matches the
    single-node branch, a LeaderWorkerSet the multi-node branch.
    """
    return any(_is_ready(entry) for entry in status_detail)
