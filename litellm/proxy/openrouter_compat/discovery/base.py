"""Base discovery adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.transport.client import DiscoveryTarget
from litellm.proxy.openrouter_compat.transport.dto import UpstreamDTO

TRaw = TypeVar("TRaw", bound=UpstreamDTO)


class BaseDiscoveryAdapter(ABC, Generic[TRaw]):
    runtime_kind: str = "unknown"

    async def discover(
        self, target: DiscoveryTarget, logical_model_name: str
    ) -> list[DiscoveredDeploymentModel]:
        raw_models = await self.discover_models(target)
        return [
            self.normalize_model(target, logical_model_name, raw)
            for raw in raw_models
        ]

    @abstractmethod
    async def discover_models(self, target: DiscoveryTarget) -> list[TRaw]:
        raise NotImplementedError

    @abstractmethod
    def normalize_model(
        self,
        target: DiscoveryTarget,
        logical_model_name: str,
        raw: TRaw,
    ) -> DiscoveredDeploymentModel:
        raise NotImplementedError