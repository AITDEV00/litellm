"""SDK-backed MCP health fixture for the connector's Go tests.

Runs the installed MCP Python SDK's StreamableHTTP transport, which allocates a
real session on initialize, enforces the Accept media rule on notifications, and
answers ping. It exposes /__status with the live session count so a Go test can
assert that the connector's health client leaves zero sessions after teardown and
after each rejected handshake.

This is a test fixture, not production code. It is launched by the Go test in
sdk_health_test.go and never shipped.

Environment:
    MCP_PORT                port to bind (required)
    MCP_REJECT_NOTIFICATION  when set, refuse notifications/initialized with 406
                            after the session was allocated, so a health client
                            must clean up the session it already created
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

from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager


async def _echo_handler(_ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(params.arguments or {}))])


async def _list_tools_handler(_ctx, _params) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[types.Tool(name="echo", description="echo", inputSchema={"type": "object"})]
    )


def main() -> None:
    port = int(os.environ["MCP_PORT"])
    # A bare SDK server already answers initialize, notifications/initialized and
    # ping with correct media-type handling; the health client only needs those.
    # The echo tool handlers let a real SDK client complete a full lifecycle
    # (initialize, list tools, call tool, terminate) on the same server the
    # health client polls.
    mcp_server: Server = Server("sdk-health-fixture")
    mcp_server.add_request_handler("tools/list", types.PaginatedRequestParams, _list_tools_handler)
    mcp_server.add_request_handler(
        "tools/call", types.CallToolRequestParams, _echo_handler
    )
    manager: StreamableHTTPSessionManager = StreamableHTTPSessionManager(
        app=mcp_server, json_response=False, stateless=False, session_idle_timeout=None
    )

    async def client_lifecycle(_request: Request) -> Response:
        """Run one real SDK client lifecycle and report the observed result."""
        outcome: dict[str, object] = {}
        async with streamable_http_client(f"http://127.0.0.1:{port}/") as (_r, _w):
            async with ClientSession(_r, _w) as session:
                await session.initialize()
                listed = await session.list_tools()
                outcome["tools"] = [tool.name for tool in listed.tools]
                called = await session.call_tool("echo", {"hello": "world"})
                outcome["content"] = [block.text for block in called.content]
        outcome["sessions"] = len(manager._server_instances)
        return JSONResponse(outcome)

    async def handle_mcp(scope, receive, send) -> None:
        # Failure mode for the regret-cleanup test: the session is allocated by
        # initialize, then the completing notification is refused at the door
        # with the same status the SDK itself uses for unacceptable requests.
        if os.environ.get("MCP_REJECT_NOTIFICATION") and scope.get("method") == "POST":
            body = b""
            more = True
            while more:
                message = await receive()
                body += message.get("body", b"")
                more = message.get("more_body", False)
            if b"notifications/initialized" in body:
                response = Response(status_code=406)
                await response(scope, receive, send)
                return
            sent = False

            async def replay() -> dict:
                nonlocal sent
                if sent:
                    return {"type": "http.disconnect"}
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}

            await manager.handle_request(scope, replay, send)
            return
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
            Route("/__client", client_lifecycle, methods=["POST"]),
            Mount("/", app=handle_mcp),
        ],
        lifespan=lifespan,
    )
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
