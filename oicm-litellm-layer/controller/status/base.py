from abc import ABC, abstractmethod
from typing import Optional

from .wire import OicmDeployment, OicmDeploymentHealth, OicmWorkloadRun


class StatusSource(ABC):
    """Reads deployment status facts for one workload.

    Abstraction over the status backend (OICM today). Implementations translate
    their native payloads into the typed ``models`` so call sites never depend
    on the OICM REST shape; a backend change is confined to one implementation.
    """

    @abstractmethod
    async def get_deployment(self, workspace_id: str, workload_id: str) -> OicmDeployment:
        ...

    @abstractmethod
    async def get_deployment_health(
        self, workspace_id: str, workload_id: str
    ) -> OicmDeploymentHealth:
        ...

    @abstractmethod
    async def get_workload_run(
        self, workspace_id: str, workload_id: str, workload_run_id: str
    ) -> Optional[OicmWorkloadRun]:
        ...

    @abstractmethod
    async def list_deployments(self, workspace_id: str) -> list[OicmDeployment]:
        ...
