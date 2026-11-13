from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Final

import httpx
from pydantic import ValidationError

from litellm.proxy.mcp_source_broker.keys import Keys
from litellm.proxy.mcp_source_broker.models import RelayMember, canonical_spec
from litellm.proxy.mcp_source_broker.store import SourceStore


async def _catalog_fingerprint(session: httpx.AsyncClient, member: RelayMember) -> str | None:
    try:
        response: Final = await session.get(f"http://{member.relay_host}:{member.relay_port}/openapi.json")
        if response.status_code != 200:
            return None
        return hashlib.sha256(canonical_spec(response.content).encode()).hexdigest()
    except (httpx.HTTPError, UnicodeDecodeError, ValidationError, ValueError):
        return None


async def probe_once(
    store: SourceStore,
    client: httpx.AsyncBaseTransport | None = None,
    keyring: Keys | None = None,
) -> None:
    keys: Final = keyring or store.keyring
    async with httpx.AsyncClient(transport=client, timeout=httpx.Timeout(5.0)) as session:
        for relay in await store.users():
            fingerprint = await _catalog_fingerprint(session, relay)
            await store.probe_connector(
                relay.connector_id,
                relay.source_id,
                keys.digest(relay.enrollment_id, relay.connector_id),
                fingerprint,
            )


@asynccontextmanager
async def probe_loop(store: SourceStore, interval_seconds: float = 10) -> AsyncGenerator[None]:
    async def periodic() -> None:
        while True:
            await probe_once(store)
            await asyncio.sleep(interval_seconds)

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(periodic())
        yield
