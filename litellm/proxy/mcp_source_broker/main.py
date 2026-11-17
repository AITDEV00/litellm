import asyncio
import os
import typing
from typing import Final

os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"

import uvicorn
from prisma import Prisma

from litellm.proxy.mcp_source_broker.keys import Keys
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
    await db.execute_raw('UPDATE "OicmMcpRemoteWorker" SET healthy_at=NULL')
    if os.environ.get("MCP_SOURCE_LINK_ENABLED") != "true":
        raise RuntimeError("a source-link transport must be enabled")
    from litellm.proxy.mcp_source_broker.remote_gateway import create_remote_control_app

    remote: Final = create_remote_control_app(store, os.environ["MCP_SOURCE_BROKER_SERVICE_TOKEN"])
    server: Final = uvicorn.Server(uvicorn.Config(remote, host="0.0.0.0", port=9091))
    try:
        await server.serve()
    finally:
        await db.disconnect()


if __name__ == "__main__":
    asyncio.run(serve())
