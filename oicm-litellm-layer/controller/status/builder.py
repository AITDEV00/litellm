"""Build an ``OicmStatusSnapshot`` from a deployment-summary item.

Pure mapping. No HTTP, no LiteLLM. Transition fields (``previous_*``,
``status_changed_at``) come from the caller's stored last snapshot, not from
OICM ``_updated_at``: that timestamp can move for reasons unrelated to status.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, TypeVar

from .availability import is_deployment_available
from .snapshot import DeploymentStatus, OicmStatusSnapshot
from .wire import DeploymentStatusDetail, OicmDeploymentSummary, StatusDetail

logger = logging.getLogger("oicm-discovery")

_E = TypeVar("_E", bound=Enum)

# Terminal statuses that are never serving, regardless of any status_detail a
# deleted/completed workload may still carry. Load-bearing: a Stopped
# deployment returns an empty status_detail, so ``status`` is what makes the
# availability answer correct rather than accidentally False.
_NOT_SERVING_STATUSES = frozenset({DeploymentStatus.STOPPED, DeploymentStatus.FAILED})


def _enum_or_none(enum_cls: type[_E], value: Optional[str]) -> Optional[_E]:
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        logger.debug("unknown %s value from OICM: %r", enum_cls.__name__, value)
        return None


def _deployment_meta_field(
    status_detail: tuple[StatusDetail, ...], field: str
) -> Optional[int]:
    """Read one int field off the Deployment entry's metadata, if present."""
    for entry in status_detail:
        if isinstance(entry, DeploymentStatusDetail) and entry.metadata is not None:
            return getattr(entry.metadata, field)
    return None


def build_snapshot(
    *,
    workspace_id: str,
    summary: OicmDeploymentSummary,
    previous: Optional[OicmStatusSnapshot],
    now: Optional[datetime] = None,
) -> OicmStatusSnapshot:
    observed = (now or datetime.now(timezone.utc)).isoformat()

    source_status = _enum_or_none(DeploymentStatus, summary.status)
    serving_available = (
        False
        if source_status in _NOT_SERVING_STATUSES
        else is_deployment_available(summary.status_detail)
    )

    # ``status_changed_at`` tracks both the source status and the serving
    # signal: a pod dropping out of service while OICM still reports ``Ready``
    # is the transition a gateway_status consumer cares about most.
    status_changed_at = previous.status_changed_at if previous else None
    if (
        previous is None
        or previous.source_status != source_status
        or previous.serving_available != serving_available
    ):
        status_changed_at = observed

    return OicmStatusSnapshot(
        workspace_id=workspace_id,
        workload_id=summary.deployment_id,
        source_status=source_status,
        desired_replicas=summary.replicas,
        available_replicas=_deployment_meta_field(summary.status_detail, "available_replicas"),
        unavailable_replicas=_deployment_meta_field(
            summary.status_detail, "unavailable_replicas"
        ),
        serving_available=serving_available,
        error_msg=summary.error_msg,
        source_updated_at=summary.updated_at,
        previous_source_status=previous.source_status if previous else None,
        status_changed_at=status_changed_at,
        observed_at=observed,
    )
