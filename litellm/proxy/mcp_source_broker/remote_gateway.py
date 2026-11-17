from __future__ import annotations

import hmac
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Annotated, Final, Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request

from litellm.proxy._types import LitellmUserRoles
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.mcp_source_broker.models import (
    LifecycleRequest,
    RemoteGoPeer,
    RemoteHealthReport,
    RemoteLifecycle,
    RemotePeer,
    RemotePeerResponse,
    RouteSelection,
    TransportName,
)
from litellm.proxy.mcp_source_broker.store import SourceStore


async def administrator(user: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)]) -> UserAPIKeyAuth:
    if user.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(403, "proxy administrator required")
    return user


async def service_authenticated(
    secret: str, authorization: Annotated[str | None, Header(alias="authorization")] = None
) -> None:
    if not hmac.compare_digest(authorization or "", f"Bearer {secret}"):
        raise HTTPException(401, "service authentication required")


async def revoke_remote_worker(
    service_secret: str,
    worker_id: str,
    source_id: str,
    control_url: str,
    transport: httpx.AsyncBaseTransport | None,
) -> None:
    headers: Final = {
        "Authorization": f"Bearer {service_secret}",
        "X-Source-Member": worker_id,
        "X-Source-Id": source_id,
    }
    async with httpx.AsyncClient(base_url=control_url, transport=transport, timeout=5.0) as client:
        result: Final = await client.post("/v1/source-link/revoke", headers=headers)
    if result.status_code != 204:
        raise HTTPException(503, "connector revocation could not be applied immediately")


LIFECYCLE_ACTIONS: Final[Mapping[str, Literal["drain", "revoke", "recover"]]] = {
    "drain": "drain",
    "revoke": "revoke",
    "recover": "recover",
}


def create_remote_gateway_router(
    store: SourceStore,
    service_secret: str,
    *,
    route: Callable[[str, TransportName], Awaitable[None]],
    control_url: str = "http://litellm-mcp-broker-dev.adeo-litellm.svc.cluster.local:9091",
    transport: httpx.AsyncBaseTransport | None = None,
) -> APIRouter:
    router: Final = APIRouter()

    @router.get("/mcp-source-link-sources/{source_id}/selection", response_model=RouteSelection)
    async def selection(  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        source_id: str, user: Annotated[UserAPIKeyAuth, Depends(administrator)]
    ) -> RouteSelection:
        del user
        return await store.selection(source_id)

    @router.put("/mcp-source-link-sources/{source_id}/selection", response_model=RouteSelection)
    async def select(  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        source_id: str, selection: RouteSelection, user: Annotated[UserAPIKeyAuth, Depends(administrator)]
    ) -> RouteSelection:
        del user
        await route(source_id, selection.transport)
        return selection

    @router.post("/mcp-source-link-sources/{source_id}/workers", response_model=RemotePeerResponse, status_code=201)
    async def enroll_peer(  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        source_id: str, peer: RemotePeer, user: Annotated[UserAPIKeyAuth, Depends(administrator)]
    ) -> RemotePeerResponse:
        del user
        created: Final = await store.remote.create_peer(source_id, peer)
        if created is None:
            raise HTTPException(409, "source or worker label is unavailable")
        return created

    @router.post(
        "/mcp-source-link-sources/{source_id}/workers/{worker_id}/{action}",
        response_model=RemoteLifecycle,
    )
    async def lifecycle(  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        source_id: str,
        worker_id: str,
        action: str,
        request: LifecycleRequest,
        user: Annotated[UserAPIKeyAuth, Depends(administrator)],
    ) -> RemoteLifecycle:
        del user
        action_value: Final[Literal["drain", "revoke", "recover"] | None] = LIFECYCLE_ACTIONS.get(action)
        if action_value is None:
            raise HTTPException(404)
        found: Final = await store.remote.worker(worker_id)
        if found is None or found.source_id != source_id:
            raise HTTPException(404)
        changed: Final = await store.remote.lifecycle(worker_id, action_value, request.generation)
        if changed is None:
            raise HTTPException(409, "stale generation or invalid lifecycle transition")
        await revoke_remote_worker(service_secret, worker_id, source_id, control_url, transport)
        return changed

    return router


async def invalidate_local_adapter(worker_id: str, source_id: str) -> None:
    if not worker_id:
        raise HTTPException(400)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9092", timeout=5.0) as client:
        result: Final = await client.post(
            "/v1/source-link/revoke",
            headers={
                "Authorization": f"Bearer {os.environ['MCP_SOURCE_BROKER_SERVICE_TOKEN']}",
                "X-Source-Member": worker_id,
                "X-Source-Id": source_id,
            },
        )
    if result.status_code != 204:
        raise HTTPException(503)


def create_remote_control_app(
    store: SourceStore, service_secret: str, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    if not service_secret:
        raise ValueError("service secret required")
    app: Final = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        return {"status": "ok"}

    @app.post("/v1/source-link/revoke", status_code=204)
    async def revoke_local(request: Request) -> None:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        if not service_secret:
            raise HTTPException(503)
        authorization: Final = request.headers.get("authorization", "")
        if not hmac.compare_digest(authorization, f"Bearer {service_secret}"):
            raise HTTPException(401)
        member: Final = request.headers.get("x-source-member", "")
        source: Final = request.headers.get("x-source-id", "")
        await invalidate_local_adapter(member, source)

    from functools import partial

    depends: Final = [Depends(partial(service_authenticated, service_secret))]

    @app.get("/v1/source-link/peers", response_model=list[RemoteGoPeer], dependencies=depends)
    async def peers() -> Sequence[RemoteGoPeer]:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        return await store.remote.peers()

    @app.post("/v1/source-link/report", dependencies=depends)
    async def report(payload: RemoteHealthReport) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        admitted: Final = await store.remote.report(payload.worker_id, payload.generation, payload.catalog_fingerprint)
        return {"admitted": admitted}

    return app
