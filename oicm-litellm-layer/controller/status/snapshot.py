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

    Sourced from the workspace-wide ``deployment_summary`` payload, which
    carries ``status`` and the per-Pod/Deployment ``status_detail`` for every
    deployment in one call. ``observed_at`` / ``status_changed_at`` are
    controller-computed; never taken from OICM ``_updated_at`` (which can
    reflect unrelated edits).

    Facts only. No presentation words ("online"/"degraded"); that mapping lives
    in LiteLLM's GatewayStateResolver. "Degraded" is ``source_status`` READY
    with ``serving_available`` False; the two are kept as separate facts so a
    consumer can decide.
    """

    workspace_id: str
    workload_id: str  # == deployment_id (proven)

    # The configured source that produced this snapshot. Liveness is tracked per
    # source, so a consumer needs this to look up the heartbeat that says whether
    # the status is still fresh.
    cluster: str

    source_status: Optional[DeploymentStatus]

    desired_replicas: Optional[int]
    available_replicas: Optional[int]
    unavailable_replicas: Optional[int]

    # Serving availability computed from the status_detail entries (a Pod on a
    # node reporting ready, or a LeaderWorkerSet reporting available). False for
    # the terminal STOPPED/FAILED statuses regardless of status_detail.
    serving_available: bool

    error_msg: Optional[str]

    source_updated_at: Optional[str]

    previous_source_status: Optional[DeploymentStatus]
    status_changed_at: Optional[str]
    observed_at: str
