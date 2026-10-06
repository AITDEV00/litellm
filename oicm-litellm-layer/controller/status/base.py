from abc import ABC, abstractmethod

from .wire import OicmDeploymentSummary


class StatusSource(ABC):
    """Reads deployment status facts for a workspace.

    One method, because the OICM status API has exactly one workspace-wide
    status payload (``deployment_summary``) that already carries every
    deployment's ``status`` and ``status_detail``. Fetching per deployment
    would be N+1 for data this single call returns whole.
    """

    @abstractmethod
    async def summaries(self, workspace_id: str) -> tuple[OicmDeploymentSummary, ...]:
        ...

    async def aclose(self) -> None:
        """Release transport resources. Default no-op for stateless sources."""
        return
