import hmac
import typing
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Final

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import TypeAdapter
from starlette.background import BackgroundTask

from litellm.proxy.mcp_source_broker.models import Member
from litellm.proxy.mcp_source_broker.store import SourceStore

DROP: Final = frozenset(
    (
        "authorization",
        "cookie",
        "host",
        "content-length",
        "connection",
        "proxy-connection",
        "upgrade",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailer",
        "x-litellm-api-key",
        "x-api-key",
    )
)
QUERY_BYTES: Final = TypeAdapter(bytes)


class PreConnectError(httpx.ConnectError):
    """Guarantees that this request was not sent."""


async def _forward_once(
    client: httpx.AsyncClient, request: Request, member: Member, path: str, body: bytes
) -> Response | None:
    query: Final = QUERY_BYTES.validate_python(request.scope["query_string"]).decode()
    url: Final = f"http://{member.relay_host}:{member.relay_port}/{path}" + ("?" + query if query else "")
    upstream: Final = client.build_request(
        request.method,
        url,
        headers=[(k, v) for k, v in request.headers.items() if k.lower() not in DROP],
        content=body,
    )
    try:
        raw: Final = await client.send(upstream, stream=True, follow_redirects=False)
    except httpx.TimeoutException:
        return JSONResponse({"detail": "source timed out"}, status_code=504)
    except PreConnectError:
        return None
    except httpx.ConnectError:
        return JSONResponse({"detail": "connector unavailable"}, status_code=503)
    except (httpx.NetworkError, httpx.ProtocolError):
        return JSONResponse({"detail": "source transport failed"}, status_code=503)
    raw.request = upstream
    return StreamingResponse(
        raw.aiter_raw(),
        status_code=raw.status_code,
        headers={k.lower(): v for k, v in raw.headers.items() if k.lower() not in DROP},
        background=BackgroundTask(raw.aclose),
    )


def create_pool_app(
    store: SourceStore, service_secret: str, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    if not service_secret:
        raise ValueError("MCP_SOURCE_BROKER_SERVICE_TOKEN is required")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(15.0)) as client:
            app.state.client = client
            yield

    app: Final = FastAPI(lifespan=lifespan)

    @app.api_route("/sources/{source_id}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    @app.api_route(
        "/sources/{source_id}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
    )
    async def forward(request: Request, source_id: str, path: str = "") -> Response:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        if not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {service_secret}"):
            return Response(status_code=401)
        if any(segment in {"..", "."} for segment in path.split("/")):
            return Response(status_code=400)
        body: Final = await request.body()
        if len(body) > 1024 * 1024:
            return Response(status_code=413)
        unsafe_client: Final = request.app.state.client  # pyright: ignore[reportAny]  # untyped FastAPI state
        client: Final = typing.cast(httpx.AsyncClient, unsafe_client)  # noqa: TID251  # cast-ok: lifespan constructs this client
        members: Final = await store.healthy_members(source_id, timedelta(seconds=60))
        for attempt, member in enumerate(members[:2]):
            forwarded = await _forward_once(client, request, member, path, body)
            if forwarded is not None:
                return forwarded
            if attempt == 1:
                return JSONResponse({"detail": "connector unavailable"}, status_code=503)
        return JSONResponse({"detail": "no eligible connector"}, status_code=503)

    return app
