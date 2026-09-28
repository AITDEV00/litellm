"""OICM deployment-status facts model.

Mirrors OICM's own status derivation (see mlops backend
``model_deployment/services/DeploymentService``), so the controller consumes the
same facts OICM computes rather than re-deriving readiness from Kubernetes.

Facts only. No presentation words ("online"/"degraded") belong here; that
mapping lives in LiteLLM's GatewayStateResolver.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, Optional


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
    status_detail: tuple[dict[str, Any], ...], is_multinode_inference: bool
) -> bool:
    """Mirror OICM ``DeploymentService.__is_deployment_available``.

    Multi-node: any LeaderWorkerSet entry whose metadata.available is truthy.
    Single-node: any Pod entry (v1) with a node assigned and metadata.ready.
    """

    def _meta(entry: dict[str, Any]) -> dict[str, Any]:
        meta = entry.get("metadata")
        return meta if isinstance(meta, dict) else {}

    if is_multinode_inference:
        return any(
            e.get("kind") == KIND_LEADER_WORKER_SET and bool(_meta(e).get("available"))
            for e in status_detail
        )
    return any(
        e.get("kind") == KIND_POD
        and e.get("apiVersion") == "v1"
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
    """

    workspace_id: str
    workload_id: str  # == deployment_id (proven)
    workload_run_id: Optional[str]

    source_status: Optional[DeploymentStatus]
    workload_status: Optional[WorkloadStatus]

    health_supported: bool
    is_ready: Optional[bool]
    health_message: Optional[str]

    desired_replicas: Optional[int]
    available_replicas: Optional[int]

    error_msg: Optional[str]

    source_version: Optional[int]
    source_updated_at: Optional[str]

    previous_source_status: Optional[DeploymentStatus]
    previous_workload_run_id: Optional[str]
    status_changed_at: Optional[str]
    observed_at: str
