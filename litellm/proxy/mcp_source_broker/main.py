import asyncio
import os
import typing
from typing import Final

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"

import uvicorn
from prisma import Prisma

from litellm.proxy.mcp_source_broker.control import create_control_app
from litellm.proxy.mcp_source_broker.keys import Keys
from litellm.proxy.mcp_source_broker.models import RelayConfig
from litellm.proxy.mcp_source_broker.pool import create_pool_app
from litellm.proxy.mcp_source_broker.store import Database, SourceStore


async def serve() -> None:
    if not os.environ.get("DATABASE_URL"):
        from litellm.proxy.utils import construct_database_url_from_env_vars

        url: Final = construct_database_url_from_env_vars()
        if url is None:
            raise RuntimeError("database environment is not configured")
        os.environ["DATABASE_URL"] = url
    db: Final = Prisma()
    await db.connect()
    store: Final = SourceStore(
        typing.cast(Database, db),  # noqa: TID251  # cast-ok: Prisma generated async methods satisfy this protocol
        Keys(os.environ["MCP_SOURCE_BROKER_PEPPER"]),
    )
    await store.ensure()
    config: Final = RelayConfig(
        relay_url=os.environ["MCP_SOURCE_BROKER_RELAY_URL"],
        public_url=os.environ["MCP_SOURCE_BROKER_PUBLIC_URL"],
        fingerprint=os.environ["MCP_SOURCE_BROKER_FINGERPRINT"],
        chisel_sha256=os.environ["MCP_SOURCE_BROKER_CHISEL_SHA256"],
    )
    token: Final = os.environ["MCP_SOURCE_BROKER_SERVICE_TOKEN"]
    await db.execute_raw('UPDATE "OicmMcpConnector" SET healthy_at=NULL')
    control: Final = create_control_app(store, token, config)
    pool: Final = create_pool_app(store, token)
    from litellm.proxy.mcp_source_broker.probes import probe_loop

    async with probe_loop(store):
        servers: Final = (
            uvicorn.Server(uvicorn.Config(control, host="0.0.0.0", port=8089)),
            uvicorn.Server(uvicorn.Config(pool, host="0.0.0.0", port=9090)),
        )
        try:
            await asyncio.gather(*(server.serve() for server in servers))
        finally:
            await db.disconnect()


if __name__ == "__main__":
    asyncio.run(serve())
