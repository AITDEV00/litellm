"""Reusable upstream probes."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Generic, TypeVar

from litellm.proxy.openrouter_compat.transport.client import (
    DiscoveryHTTPClient,
    DiscoveryTarget,
)
from litellm.proxy.openrouter_compat.transport.errors import DiscoveryError

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ProbeResult(Generic[T]):
    success: bool
    data: T | None
    probe: str
    latency_ms: float
    error_category: str | None = None
    error_detail: str | None = None


class BaseProbe(Generic[T]):
    name: str = "base"

    def __init__(self, client: DiscoveryHTTPClient) -> None:
        self._client = client

    async def run(self, target: DiscoveryTarget) -> ProbeResult[T]:
        start = time.monotonic()
        try:
            data = await self._fetch(target)
            latency = (time.monotonic() - start) * 1000
            return ProbeResult(
                success=True, data=data, probe=self.name, latency_ms=latency
            )
        except DiscoveryError as exc:
            latency = (time.monotonic() - start) * 1000
            return ProbeResult(
                success=False,
                data=None,
                probe=self.name,
                latency_ms=latency,
                error_category=exc.category,
                error_detail=str(exc),
            )

    async def _fetch(self, target: DiscoveryTarget) -> T:
        raise NotImplementedError