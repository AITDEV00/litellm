"""Shared async HTTP transport for upstream runtime probes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import cast

import httpx

from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.proxy.openrouter_compat.transport.errors import (
    DiscoveryConnectionError,
    DiscoveryHTTPError,
    DiscoveryInvalidJSON,
    DiscoveryTimeout,
)


@dataclass(frozen=True, slots=True)
class DiscoveryTarget:
    deployment_id: str
    api_base: str
    auth_headers: dict[str, str]


def fingerprint(value: str) -> str:
    """Deterministic short hash used for dedup/cache keys (never secrets)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class DiscoveryHTTPClient:
    """Bounded-concurrency async client for upstream runtime probes."""

    def __init__(
        self,
        *,
        connect_timeout: float = 2.0,
        read_timeout: float = 3.0,
        max_response_bytes: int = 512_000,
        max_concurrency: int = 16,
    ) -> None:
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._max_response_bytes = max_response_bytes
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._http = AsyncHTTPHandler(
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout)
        )

    async def get_json(self, target: DiscoveryTarget, path: str) -> dict[str, object]:
        url = target.api_base.rstrip("/") + path
        timeout = httpx.Timeout(self._read_timeout, connect=self._connect_timeout)
        try:
            async with self._semaphore:
                # AsyncHTTPHandler.get returns partially-unknown Response type.
                response = await self._http.get(url, headers=target.auth_headers, timeout=timeout)  # pyright: ignore[reportUnknownMemberType]
        except httpx.TimeoutException as exc:
            raise DiscoveryTimeout(f"GET {path} timed out") from exc
        except httpx.ConnectError as exc:
            raise DiscoveryConnectionError(f"GET {path} connect failed") from exc
        except httpx.HTTPError as exc:
            raise DiscoveryConnectionError(f"GET {path} failed: {exc}") from exc

        if response.status_code >= 400:
            raise DiscoveryHTTPError(response.status_code, f"GET {path}")

        body = response.content
        if len(body) > self._max_response_bytes:
            raise DiscoveryInvalidJSON(f"GET {path} response too large")
        try:
            raw_payload = cast(object, json.loads(body))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DiscoveryInvalidJSON(f"GET {path} invalid JSON") from exc
        if not isinstance(raw_payload, dict):
            raise DiscoveryInvalidJSON(f"GET {path} expected JSON object")
        return cast(dict[str, object], raw_payload)

    async def aclose(self) -> None:
        await self._http.close()