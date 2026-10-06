from abc import ABC, abstractmethod

from .wire import OicmDeploymentSummary


class StatusSource(ABC):
    """Reads deployment status facts for one OICM instance.

    One source per instance, because each has its own workspace and credentials.
    The workspace id belongs to the source rather than to the call so a poller
    can fan out over several sources without threading ids through.

    ``name`` identifies the instance in logs and in the ``GET /status`` output,
    which is what makes a snapshot attributable to a cluster once several
    sources are merged.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    @abstractmethod
    def workspace_id(self) -> str:
        ...

    @abstractmethod
    async def summaries(self) -> tuple[OicmDeploymentSummary, ...]:
        """Every deployment in this source's workspace, with its status.

        One call per source per cycle: the OICM status API has exactly one
        workspace-wide payload (``deployment_summary``) that already carries
        every deployment's ``status`` and ``status_detail``. Fetching per
        deployment would be N+1 for data this single call returns whole.
        """
        ...

    async def aclose(self) -> None:
        """Release transport resources. Default no-op for stateless sources."""
        return
