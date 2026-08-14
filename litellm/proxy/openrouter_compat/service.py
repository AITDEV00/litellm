"""Discovery orchestration (design §42). Bounded async fan-out with dedup."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from litellm.proxy.openrouter_compat.cache.memory import InMemoryDiscoveryCache
from litellm.proxy.openrouter_compat.discovery.registry import DiscoveryAdapterRegistry
from litellm.proxy.openrouter_compat.discovery.resolver import DeploymentDescriptor
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DiscoveryResult:
    """Successful discoveries plus the logical models that produced none.

    A logical model lands in ``failed_logical_models`` when every one of its
    deployments discovered zero models (network error, bad endpoint, or the
    runtime not exposing the id). Callers may then surface a placeholder
    instead of silently dropping the model.
    """

    discoveries: list[DiscoveredDeploymentModel] = field(default_factory=list)
    failed_logical_models: set[str] = field(default_factory=set)


class DiscoveryService:
    def __init__(
        self,
        registry: DiscoveryAdapterRegistry,
        cache: InMemoryDiscoveryCache | None = None,
    ) -> None:
        self._registry = registry
        self._cache = cache

    async def discover_many(self, deployments: list[DeploymentDescriptor]) -> DiscoveryResult:
        targets = self._deduplicate_targets(deployments)
        if not targets:
            return DiscoveryResult()

        async def discover_one(
            descriptor: DeploymentDescriptor,
        ) -> tuple[str, list[DiscoveredDeploymentModel]]:
            target = descriptor.to_discovery_target()
            if target is None:
                return descriptor.logical_model_name, []
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
                    return descriptor.logical_model_name, cached
            try:
                discovered = await adapter.discover(target, descriptor.logical_model_name)
            except Exception as exc:  # noqa: BLE001 - resilient partial catalog
                logger.warning(
                    "discovery failed for %s: %s",
                    descriptor.logical_model_name,
                    exc,
                )
                return descriptor.logical_model_name, []
            if self._cache is not None:
                self._cache.set(
                    descriptor.deployment_id,
                    runtime_kind,
                    target.api_base,
                    descriptor.model or "",
                    discovered,
                )
            return descriptor.logical_model_name, discovered

        results = await asyncio.gather(
            *[discover_one(t) for t in targets],
            return_exceptions=True,
        )
        flat: list[DiscoveredDeploymentModel] = []
        by_logical: dict[str, list[list[DiscoveredDeploymentModel]]] = defaultdict(list)
        for descriptor, result in zip(targets, results, strict=True):
            if isinstance(result, tuple):
                logical_name, discovered = result
                by_logical[logical_name].append(discovered)
                flat.extend(discovered)
            else:
                by_logical[descriptor.logical_model_name].append([])
        failed_logical_models = {name for name, batches in by_logical.items() if not any(batches)}
        return DiscoveryResult(
            discoveries=flat,
            failed_logical_models=failed_logical_models,
        )

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
