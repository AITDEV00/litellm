"""SDK-backed MCP health fixture for the connector's Go tests.

Runs the installed MCP Python SDK's StreamableHTTP transport, which allocates a
real session on initialize, enforces the Accept media rule on notifications, and
answers ping. It exposes /__status with the live session count so a Go test can
assert that the connector's health client leaves zero sessions after teardown and
after each rejected handshake.

This is a test fixture, not production code. It is launched by the Go test in
sdk_health_test.go and never shipped.

Environment:
    MCP_PORT           port to bind (required)
    MCP_INIT_MODE      "" (default) | reject-notification | jsonrpc-error |
                       unsupported-version | empty-result
"""

import asyncio
import contextlib
import json
import os

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager


def main() -> None:
    port = int(os.environ["MCP_PORT"])
    # A bare SDK server already answers initialize, notifications/initialized and
    # ping with correct media-type handling; the health client only needs those.
    mcp_server: Server = Server("sdk-health-fixture")
    manager: StreamableHTTPSessionManager = StreamableHTTPSessionManager(
        app=mcp_server, json_response=False, stateless=False, session_idle_timeout=None
    )

    async def handle_mcp(scope, receive, send) -> None:
        await manager.handle_request(scope, receive, send)

    async def status(_request: Request) -> Response:
        return JSONResponse({"sessions": len(manager._server_instances)})

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        async with manager.run():
            yield

    # Mount the raw ASGI manager at root: Starlette routes pass a Request, but the
    # manager needs the raw (scope, receive, send) triple, so it is mounted as an
    # ASGI app. Mounting at / with explicit sub-routes above avoids the automatic
    # 307 slash redirect that a /mcp mount would emit for /mcp vs /mcp/.
    app: Starlette = Starlette(
        routes=[
            Route("/__status", status, methods=["GET"]),
            Route("/healthz", healthz, methods=["GET"]),
            Mount("/", app=handle_mcp),
        ],
        lifespan=lifespan,
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
