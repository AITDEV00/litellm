"""Discovery orchestration (design §42). Bounded async fan-out with dedup."""

from __future__ import annotations

import asyncio
import logging

from litellm.proxy.openrouter_compat.cache.memory import InMemoryDiscoveryCache
from litellm.proxy.openrouter_compat.discovery.registry import DiscoveryAdapterRegistry
from litellm.proxy.openrouter_compat.discovery.resolver import DeploymentDescriptor
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel

logger = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(
        self,
        registry: DiscoveryAdapterRegistry,
        cache: InMemoryDiscoveryCache | None = None,
    ) -> None:
        self._registry = registry
        self._cache = cache

    async def discover_many(
        self, deployments: list[DeploymentDescriptor]
    ) -> list[DiscoveredDeploymentModel]:
        targets = self._deduplicate_targets(deployments)
        if not targets:
            return []

        async def discover_one(
            descriptor: DeploymentDescriptor,
        ) -> list[DiscoveredDeploymentModel]:
            target = descriptor.to_discovery_target()
            if target is None:
                return []
            adapter = self._registry.resolve(descriptor)
            runtime_kind = adapter.runtime_kind
            if self._cache is not None:
                cached = self._cache.get(
                    descriptor.deployment_id,
                    runtime_kind,
                    target.api_base,
                    descriptor.model or "",
                )
                if cached is not None:
                    return cached
            try:
                discovered = await adapter.discover(target, descriptor.logical_model_name)
            except Exception as exc:  # noqa: BLE001 - resilient partial catalog
                logger.warning(
                    "discovery failed for %s: %s",
                    descriptor.logical_model_name,
                    exc,
                )
                return []
            if self._cache is not None:
                self._cache.set(
                    descriptor.deployment_id,
                    runtime_kind,
                    target.api_base,
                    descriptor.model or "",
                    discovered,
                )
            return discovered

        results = await asyncio.gather(
            *[discover_one(t) for t in targets],
            return_exceptions=True,
        )
        flat: list[DiscoveredDeploymentModel] = []
        for result in results:
            if isinstance(result, list):
                flat.extend(result)
        return flat

    @staticmethod
    def _deduplicate_targets(
        deployments: list[DeploymentDescriptor],
    ) -> list[DeploymentDescriptor]:
        seen: set[tuple[str, str]] = set()
        deduped: list[DeploymentDescriptor] = []
        for descriptor in deployments:
            key = (descriptor.deployment_id, descriptor.api_base or "")
            if key in seen:
                continue
            seen.add(key)
            deduped.append(descriptor)
        return deduped