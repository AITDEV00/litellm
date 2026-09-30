"""Typed OICM status payloads.

These models validate the OICM REST responses at the client boundary, so the
rest of the controller works with typed objects instead of ``dict[str, Any]``.
Field names match the OICM wire format; ``extra="allow"`` keeps forward-
compatible fields OICM adds later without breaking parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final, Optional

from pydantic import BaseModel, ConfigDict, Field


class DeploymentStatus(str, Enum):
    """OICM ``DeploymentStatus`` (app/repositories/deployment/definitions)."""

    PENDING = "Pending"
    DEPLOYING = "Deploying"
    AVAILABLE = "Available"
    READY = "Ready"
    STOPPED = "Stopped"
    FAILED = "Failed"
    UNDEPLOYING = "Undeploying"


class WorkloadStatus(str, Enum):
    """OICM ``WorkloadStatus``; drives ``DeploymentStatus`` in refresh_status."""

    CREATING = "Creating"
    PENDING = "Pending"
    INITIALIZING = "Initializing"
    RUNNING = "Running"
    FAILED = "Failed"
    DELETING = "Deleting"
    COMPLETED = "Completed"


# status_detail entry kinds present in a workload run.
KIND_DEPLOYMENT: Final = "Deployment"
KIND_POD: Final = "Pod"
KIND_LEADER_WORKER_SET: Final = "LeaderWorkerSet"


def is_deployment_available(
    status_detail: tuple[dict[str, object], ...], is_multinode_inference: bool
) -> bool:
    """Serving availability derived from a workload run's ``status_detail``.

    Mirrors OICM ``DeploymentService.__is_deployment_available`` in intent:
    multi-node = any LeaderWorkerSet entry with ``metadata.available``; single-node
    = any Pod with a node assigned and ``metadata.ready``.

    Deliberately does NOT require the Pod's ``apiVersion == "v1"``. OICM's own
    check does, but the OICM API currently returns ``apiVersion: null`` for every
    ``status_detail`` entry, which makes OICM's availability (and hence
    ``/health.is_ready``) report False for every deployment. A Running pod on a
    node with ``ready: true`` is serving; ``apiVersion`` is not populated by this
    API, so it must not be a precondition.
    """

    def _meta(entry: dict[str, object]) -> dict[str, object]:
        meta = entry.get("metadata")
        return meta if isinstance(meta, dict) else {}

    if is_multinode_inference:
        return any(
            e.get("kind") == KIND_LEADER_WORKER_SET and bool(_meta(e).get("available"))
            for e in status_detail
        )
    return any(
        e.get("kind") == KIND_POD
        and bool(e.get("node"))
        and bool(_meta(e).get("ready"))
        for e in status_detail
    )


@dataclass(frozen=True, slots=True)
class OicmStatusSnapshot:
    """One observed status snapshot for a single OICM model deployment.

    Populated from the deployment, its health, and optionally its workload run.
    ``observed_at`` / ``status_changed_at`` are controller-computed; never taken
    from OICM ``_updated_at`` (which can reflect unrelated edits).

    Facts only. No presentation words ("online"/"degraded"); that mapping lives
    in LiteLLM's GatewayStateResolver.

    ``is_ready`` is the OICM ``/health`` readiness boolean. It is ADVISORY ONLY:
    OICM recomputes it from a celery task that fires only on deploy/scale/run
    lifecycle events (no periodic sweep), so it lags real pod state after a
    self-heal. Use ``serving_available`` (computed from the workload run's
    ``status_detail``, the live signal) for the availability decision instead.
    """

    workspace_id: str
    workload_id: str  # == deployment_id (proven)
    workload_run_id: Optional[str]

    source_status: Optional[DeploymentStatus]
    workload_status: Optional[WorkloadStatus]

    health_supported: bool
    is_ready: Optional[bool]  # advisory only; see serving_available
    health_message: Optional[str]

    desired_replicas: Optional[int]
    available_replicas: Optional[int]

    # Serving availability computed from the workload run's status_detail (the
    # accurate, continuously-updated signal), NOT from /health.is_ready (stale).
    # None when no workload run is known.
    serving_available: Optional[bool]

    error_msg: Optional[str]

    source_version: Optional[int]
    source_updated_at: Optional[str]

    previous_source_status: Optional[DeploymentStatus]
    previous_workload_run_id: Optional[str]
    status_changed_at: Optional[str]
    observed_at: str


class OicmDeployment(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    workspace_id: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    error_msg: Optional[str] = None
    replicas: Optional[int] = None
    enable_auto_scaling: Optional[bool] = None
    version: Optional[int] = Field(default=None, alias="_version")
    updated_at: Optional[str] = Field(default=None, alias="_updated_at")


class OicmDeploymentHealth(BaseModel):
    model_config = ConfigDict(extra="allow")

    is_health_check_supported: Optional[bool] = None
    is_ready: Optional[bool] = None
    message: Optional[str] = None


class OicmStatusDetail(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Optional[str] = None
    apiVersion: Optional[str] = None
    name: Optional[str] = None
    node: Optional[str] = None
    status: Optional[str] = None
    status_msg: Optional[str] = None
    metadata: Optional[dict[str, object]] = None


class OicmWorkloadRun(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    workload_id: Optional[str] = None
    workspace_id: Optional[str] = None
    workload_status: Optional[str] = None
    status_detail: tuple[OicmStatusDetail, ...] = ()

    @property
    def ready_pod_count(self) -> int:
        return sum(
            1
            for e in self.status_detail
            if e.kind == KIND_POD and isinstance(e.metadata, dict) and e.metadata.get("ready")
        )
