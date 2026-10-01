"""Build an ``OicmStatusSnapshot`` from wire models + transition memory.

Pure mapping. No HTTP, no LiteLLM. Transition fields (``previous_*``,
``status_changed_at``) come from the caller's stored last snapshot, not from
OICM ``_updated_at`` — that timestamp can move for reasons unrelated to status.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional, TypeVar

from .availability import is_deployment_available
from .snapshot import DeploymentStatus, OicmStatusSnapshot, WorkloadStatus
from .wire import OicmDeployment, OicmDeploymentHealth, OicmWorkloadRun

_E = TypeVar("_E", bound=Enum)


def _enum_or_none(enum_cls: type[_E], value: Optional[str]) -> Optional[_E]:
    if value is None:
        return None
    try:
        return enum_cls(value)
    except ValueError:
        return None


def _serving_available(
    source_status: Optional[DeploymentStatus],
    workload_run: Optional[OicmWorkloadRun],
) -> Optional[bool]:
    # Terminal non-serving state: definitively not available, regardless of
    # whether a (deleted/completed) run with status_detail still exists.
    if source_status in (DeploymentStatus.STOPPED, DeploymentStatus.FAILED):
        return False
    if workload_run is None:
        return None
    return is_deployment_available(workload_run.status_detail)


def build_snapshot(
    *,
    workspace_id: str,
    workload_id: str,
    workload_run_id: Optional[str],
    deployment: OicmDeployment,
    health: Optional[OicmDeploymentHealth],
    workload_run: Optional[OicmWorkloadRun],
    previous: Optional[OicmStatusSnapshot],
    now: Optional[datetime] = None,
) -> OicmStatusSnapshot:
    observed = (now or datetime.now(timezone.utc)).isoformat()

    source_status = _enum_or_none(DeploymentStatus, deployment.status)
    workload_status = _enum_or_none(
        WorkloadStatus, workload_run.workload_status if workload_run else None
    )

    status_changed_at = previous.status_changed_at if previous else None
    if previous is None or previous.source_status != source_status:
        status_changed_at = observed

    return OicmStatusSnapshot(
        workspace_id=workspace_id,
        workload_id=workload_id,
        workload_run_id=workload_run_id,
        source_status=source_status,
        workload_status=workload_status,
        health_supported=bool(health and health.is_health_check_supported),
        is_ready=health.is_ready if health else None,
        health_message=health.message if health else None,
        desired_replicas=deployment.replicas,
        available_replicas=workload_run.ready_pod_count if workload_run else None,
        serving_available=_serving_available(source_status, workload_run),
        error_msg=deployment.error_msg,
        source_version=deployment.version,
        source_updated_at=deployment.updated_at,
        previous_source_status=previous.source_status if previous else None,
        previous_workload_run_id=previous.workload_run_id if previous else None,
        status_changed_at=status_changed_at,
        observed_at=observed,
    )
