"""In-memory TTL cache for discovery results (design §34).

Caches raw discovered deployments per discovery target, never per caller, so
caller authorization/aggregation is always applied after a cache hit. No
persistent DB.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.transport.client import fingerprint


@dataclass(frozen=True, slots=True)
class CacheKey:
    deployment_id: str
    runtime_kind: str
    api_base_fingerprint: str
    auth_fingerprint: str


@dataclass(frozen=True, slots=True)
class _Entry:
    value: list[DiscoveredDeploymentModel]
    expires_at: float


class InMemoryDiscoveryCache:
    """Thread-safe TTL cache keyed by discovery target (not by caller)."""

    def __init__(self, ttl_seconds: float = 15.0, max_entries: int = 512) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._data: OrderedDict[str, _Entry] = OrderedDict()

    def key(
        self,
        deployment_id: str,
        runtime_kind: str,
        api_base: str,
        auth_identity: str,
    ) -> str:
        return "|".join(
            (
                deployment_id,
                runtime_kind,
                fingerprint(api_base),
                fingerprint(auth_identity),
            )
        )

    def get(
        self,
        deployment_id: str,
        runtime_kind: str,
        api_base: str,
        auth_identity: str,
    ) -> list[DiscoveredDeploymentModel] | None:
        key = self.key(deployment_id, runtime_kind, api_base, auth_identity)
        entry = self._data.get(key)
        if entry is None:
            return None
        if time.monotonic() > entry.expires_at:
            self._data.pop(key, None)
            return None
        return entry.value

    def set(
        self,
        deployment_id: str,
        runtime_kind: str,
        api_base: str,
        auth_identity: str,
        value: list[DiscoveredDeploymentModel],
    ) -> None:
        key = self.key(deployment_id, runtime_kind, api_base, auth_identity)
        self._data[key] = _Entry(
            value=value, expires_at=time.monotonic() + self._ttl
        )
        self._data.move_to_end(key)
        while len(self._data) > self._max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()