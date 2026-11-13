from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import sys
import tempfile
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Final, TypeVar

import httpx
from pydantic import TypeAdapter, ValidationError

from litellm.proxy.mcp_source_broker.models import RelayUser

DEFAULT_AUTHFILE: Final = Path("/tmp/mcp-source-broker/chisel-users.json")
DEFAULT_BROKER_URL: Final = "http://litellm-mcp-broker-dev.adeo-litellm.svc.cluster.local:8089"
RELAY_USERS: Final = TypeAdapter(tuple[RelayUser, ...])
AUTHFILE: Final = TypeAdapter(dict[str, tuple[str, ...]])


class DuplicateUsername(ValueError):
    pass


Value: Final = TypeVar("Value")


def unique_mapping(pairs: Iterable[tuple[str, Value]]) -> Mapping[str, Value]:
    validated: Final = tuple(pairs)
    result: Final = MappingProxyType(dict(validated))
    if len(result) != len(validated):
        raise DuplicateUsername("relay usernames must be unique")
    return result


def _validate_user(user: RelayUser) -> None:
    address: Final = ipaddress.ip_address(user.relay_host)
    if address not in ipaddress.ip_network("127.0.0.0/24") or not 20 <= int(address) % 256 <= 254:
        raise ValueError("relay listeners must use allocated IPv4 127.0.0.20-127.0.0.254 addresses")
    if not all(
        part.isascii() and not any(character.isspace() for character in part) for part in (user.username, user.password)
    ):
        raise ValueError("relay credentials must be non-empty single-line ASCII")
    if not user.username or not user.password:
        raise ValueError("relay credentials must not be empty")
    if not 18080 <= user.relay_port <= 18120 or not 1 <= user.target_port <= 65535:
        raise ValueError("relay port is invalid")


def authfile_content(users: Sequence[RelayUser]) -> bytes:
    for user in users:
        _validate_user(user)
    records: Final = (
        (
            f"{user.username}:{user.password}",
            (f"^{re.escape(f'R:{user.relay_host}:{user.relay_port}')}$",),
        )
        for user in users
    )
    content: Final = AUTHFILE.validate_python(unique_mapping(records))
    unique_mapping((user.username, user) for user in users)
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def write_authfile(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        path.chmod(0o600)
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".chisel-users-", delete=False) as stream:
        temporary: Final = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


async def load_users(client: httpx.AsyncClient) -> Sequence[RelayUser]:
    response: Final = await client.get("/v1/relay-users")
    response.raise_for_status()
    return RELAY_USERS.validate_json(response.content)


async def refresh_authfile(client: httpx.AsyncClient, path: Path) -> None:
    write_authfile(path, authfile_content(await load_users(client)))


@asynccontextmanager
async def control_client(
    broker_url: str | None = None,
    service_token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncGenerator[httpx.AsyncClient]:
    base_url: Final = broker_url or os.environ.get("MCP_SOURCE_BROKER_URL", DEFAULT_BROKER_URL)
    token: Final = service_token if service_token is not None else os.environ["MCP_SOURCE_BROKER_SERVICE_TOKEN"]
    if not token:
        raise ValueError("MCP_SOURCE_BROKER_SERVICE_TOKEN is required")
    async with httpx.AsyncClient(
        base_url=base_url,
        transport=transport,
        timeout=httpx.Timeout(10.0),
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client


async def run_authfile_loop(
    path: Path | None = None,
    *,
    broker_url: str | None = None,
    service_token: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    interval_seconds: float = 10,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    destination: Final = path or Path(os.environ.get("MCP_SOURCE_BROKER_AUTHFILE", DEFAULT_AUTHFILE))
    async with control_client(broker_url, service_token, transport) as client:
        while True:
            try:
                await refresh_authfile(client, destination)
            except (httpx.HTTPError, ValidationError, ValueError, OSError) as error:
                logging.getLogger(__name__).error("Relay authfile refresh failed: %s", type(error).__name__)
            await sleep(interval_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--once" in sys.argv[1:]:

        async def initial_refresh() -> None:
            async with control_client() as client:
                await refresh_authfile(client, Path(os.environ.get("MCP_SOURCE_BROKER_AUTHFILE", DEFAULT_AUTHFILE)))

        asyncio.run(initial_refresh())
        return
    asyncio.run(run_authfile_loop())


if __name__ == "__main__":
    main()
