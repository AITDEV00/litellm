from __future__ import annotations

import asyncio
import os
import typing
from collections.abc import Awaitable, Callable
from typing import Annotated, Final, Protocol, TypeAlias

from fastapi import APIRouter, Depends, HTTPException

from litellm.proxy._experimental.mcp_server.db import (
    McpIdentifierConflict,
    create_mcp_server_if_identifier_free,
    get_mcp_server,
    update_mcp_server,
)
from litellm.proxy._experimental.mcp_server.mcp_server_manager import MCPServerManager
from litellm.proxy._types import (
    LiteLLM_MCPServerTable,
    LitellmUserRoles,
    NewMCPServerRequest,
    UpdateMCPServerRequest,
)
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.client_distribution.storage import (
    create_s3_client,  # pyright: ignore[reportUnknownVariableType]  # optional boto3 stubs
    load_config_from_env,
)
from litellm.proxy.mcp_source_broker.keys import Keys
from litellm.proxy.mcp_source_broker.models import SourceCreate
from litellm.proxy.mcp_source_broker.remote_gateway import create_remote_gateway_router
from litellm.proxy.mcp_source_broker.store import Database, NotOwnerConflict, SourceStore
from litellm.proxy.utils import PrismaClient
from litellm.types.mcp import MCPAuth, MCPTransport


class MountableApp(Protocol):
    def include_router(self, router: APIRouter) -> None: ...


FindMCPServer: TypeAlias = Callable[[PrismaClient, str], Awaitable[LiteLLM_MCPServerTable | None]]


class S3PutClient(Protocol):
    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> object: ...


class MCPCreate(Protocol):
    async def __call__(
        self, prisma_client: PrismaClient, data: NewMCPServerRequest, touched_by: str
    ) -> LiteLLM_MCPServerTable | McpIdentifierConflict: ...


class MCPUpdate(Protocol):
    async def __call__(
        self,
        prisma_client: PrismaClient,
        data: UpdateMCPServerRequest,
        touched_by: str,
    ) -> LiteLLM_MCPServerTable | McpIdentifierConflict | None: ...


RouteSource: TypeAlias = Callable[[str], Awaitable[None]]


class ArtifactStore(Protocol):
    async def __call__(self, key: str, body: bytes) -> None: ...


class CatalogConfig(Protocol):
    def catalog_url(self, path: str) -> str: ...


def create_gateway_router(
    store: SourceStore,
    register: Callable[[NewMCPServerRequest], Awaitable[None]],
    catalog: CatalogConfig,
    service_secret: str,
    *,
    data_plane_url: str = "http://litellm-mcp-broker-dev.adeo-litellm.svc.cluster.local:9092",
    artifacts: ArtifactStore | None = None,
) -> APIRouter:
    if not service_secret:
        raise ValueError("MCP_SOURCE_BROKER_SERVICE_TOKEN is required")
    router: Final = APIRouter()

    async def put_artifact(key: str, body: bytes) -> None:
        if artifacts is not None:
            await artifacts(key, body)
            return
        config: Final = load_config_from_env()
        client: Final = typing.cast(  # noqa: TID251  # cast-ok: optional boto3 stubs
            S3PutClient,
            create_s3_client(config),
        )  # pyright: ignore[reportUnknownVariableType]  # optional boto3 stubs
        await asyncio.to_thread(
            lambda: client.put_object(Bucket=config.bucket, Key=key, Body=body, ContentType="application/json")
        )

    @router.post("/mcp-sources", status_code=201)
    async def enroll(  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        payload: SourceCreate, user: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)]
    ) -> dict[str, str]:
        if user.user_role != LitellmUserRoles.PROXY_ADMIN:
            raise HTTPException(403, "proxy administrator required")
        owner: Final = user.user_id or payload.owner
        request: Final = payload.with_owner(owner)
        await store.ensure()
        result: Final = await store.enroll(request, owner)
        if isinstance(result, NotOwnerConflict):
            raise HTTPException(409, "source owner or alias conflicts")
        await put_artifact(result.spec_path, request.spec.encode())
        await register(
            NewMCPServerRequest(
                server_id=result.source_id,
                server_name=result.alias,
                alias=result.alias,
                url=f"{data_plane_url}/sources/{result.source_id}",
                spec_path=catalog.catalog_url(result.spec_path),
                mcp_info={"owner": result.tenant},
                mcp_access_groups=[f"mcp-source-{result.source_id}"],
                transport=MCPTransport.http,
                auth_type=MCPAuth.bearer_token,
                credentials={"auth_value": service_secret},
            )
        )
        return {"source_id": result.source_id, "status": result.status}

    return router


def mount_mcp_sources(
    app: MountableApp,
    prisma: PrismaClient,
    manager: MCPServerManager,
    store: SourceStore | None = None,
) -> None:
    token: Final = os.environ["MCP_SOURCE_BROKER_SERVICE_TOKEN"]
    source_store: Final = store or SourceStore(
        typing.cast(  # noqa: TID251  # cast-ok: wrapper delegates the SQL protocol to generated Prisma
            Database,
            prisma.writer_db,
        ),
        Keys(os.environ["MCP_SOURCE_BROKER_PEPPER"]),
    )

    async def create(request: NewMCPServerRequest) -> None:
        await default_registration(prisma, manager, request)

    async def route_source(source_id: str) -> None:
        found: Final = await get_mcp_server(prisma, source_id)
        if found is None:
            raise ValueError("source registration is missing")
        await source_store.route(source_id, "remotedialer")
        await update_mcp_server(
            prisma,
            UpdateMCPServerRequest(
                server_id=source_id,
                url=f"http://litellm-mcp-broker-dev.adeo-litellm.svc.cluster.local:9092/sources/{source_id}",
            ),
            touched_by=found.mcp_info.get("owner", "mcp-source-broker") if found.mcp_info else "mcp-source-broker",
        )
        await manager.reload_servers_from_database()

    app.include_router(create_gateway_router(source_store, create, _catalog_config(), token))
    app.include_router(
        create_remote_gateway_router(
            source_store,
            token,
            route=route_source,
        )
    )


class EnvCatalogConfig:
    def catalog_url(self, path: str) -> str:
        base: Final = os.environ.get("MCP_SOURCE_BROKER_CATALOG_BASE", "http://127.0.0.1:4000/install")
        return f"{base.rstrip('/')}/{path}"


def _catalog_config() -> CatalogConfig:
    return EnvCatalogConfig()


async def default_registration(
    prisma: PrismaClient,
    manager: MCPServerManager,
    request: NewMCPServerRequest,
    *,
    find: FindMCPServer = get_mcp_server,
    create: MCPCreate = create_mcp_server_if_identifier_free,
    update: MCPUpdate = update_mcp_server,
) -> None:
    source_id: Final = request.server_id or ""
    owner: Final = request.mcp_info.get("owner", "mcp-source-broker") if request.mcp_info else "mcp-source-broker"
    result: Final = (
        await create(prisma, request, owner)
        if await find(prisma, source_id) is None
        else await update(
            prisma,
            UpdateMCPServerRequest.model_validate(request.model_dump(exclude_unset=True)),
            touched_by=owner,
        )
    )
    if isinstance(result, McpIdentifierConflict):
        raise TypeError("MCP identifier conflict")
    if result is None:
        raise RuntimeError("source registration is missing")
    await manager.reload_servers_from_database()


def mount_mcp_sources_when_enabled(
    app: MountableApp,
    prisma: PrismaClient,
    manager: MCPServerManager,
    store: SourceStore | None = None,
) -> None:
    if os.environ.get("MCP_SOURCE_BROKER_ENABLED") != "true":
        return
    mount_mcp_sources(app, prisma, manager, store)
