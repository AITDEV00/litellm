"""Serving availability derived from a workload run's ``status_detail``.

Mirrors OICM ``DeploymentService.__is_deployment_available`` in intent:
multi-node = any LeaderWorkerSet entry with ``metadata.available``; single-node
= any Pod with a node assigned and ``metadata.ready``.

Deliberately does NOT require the Pod's ``apiVersion == "v1"``. OICM's own check
does, but the OICM API currently returns ``apiVersion: null`` for every
``status_detail`` entry, which makes OICM's availability (and hence
``/health.is_ready``) report False for every deployment. A Running pod on a node
with ``ready: true`` is serving; ``apiVersion`` is not populated by this API, so
it must not be a precondition.

The match is over the typed discriminated-union variants, so a new OICM ``kind``
falls through to ``False`` instead of being silently misread.
"""

from __future__ import annotations

from .wire import (
    DeploymentStatusDetail,
    LeaderWorkerSetStatusDetail,
    PodStatusDetail,
    StatusDetail,
)


def _is_ready(entry: StatusDetail) -> bool:
    match entry:
        case PodStatusDetail(node=node, metadata=meta):
            return bool(node) and meta is not None and bool(meta.ready)
        case LeaderWorkerSetStatusDetail(metadata=meta):
            return meta is not None and bool(meta.available)
        case DeploymentStatusDetail():
            return False


def is_deployment_available(status_detail: tuple[StatusDetail, ...]) -> bool:
    """True when any status_detail entry reports a serving-ready object.

    Works for either topology without a topology flag: a Pod entry only matches
    the single-node branch, a LeaderWorkerSet only the multi-node branch.
    """
    return any(_is_ready(entry) for entry in status_detail)
