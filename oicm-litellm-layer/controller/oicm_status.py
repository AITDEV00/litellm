"""Backwards-compatible re-exports. Prefer ``controller.status``.

The status facts model, the StatusSource abstraction, and the OICM-backed
implementation moved into the ``controller.status`` package. This shim keeps any
``from controller.oicm_status import ...`` reference working.
"""

from .status import (  # noqa: F401
    DeploymentStatus,
    OicmStatusSnapshot,
    WorkloadStatus,
    is_deployment_available,
)