"""Controller-internal status snapshot, decoupled from the OICM wire format.

The wire DTOs live in ``wire``; this is the controller's own typed view. Keeping
the two as separate types means an OICM API change only ripples as far as the
pydantic boundary and ``builder``; nothing downstream touches OICM JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


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
