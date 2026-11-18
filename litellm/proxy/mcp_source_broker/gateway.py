from __future__ import annotations

import asyncio
import os
import typing
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated, Final, Protocol, TypeAlias

from fastapi import APIRouter, Depends, HTTPException

from litellm.proxy._experimental.mcp_server.db import (
    McpIdentifierConflict,
    create_mcp_server_if_identifier_free,
    get_mcp_server,
    update_mcp_server,
)
from litellm.proxy._experimental.mcp_server.mcp_server_manager import MCPServerManager
from litellm.proxy._experimental.mcp_server.utils import (
    validate_and_normalize_mcp_server_payload,
)
from litellm.proxy._types import (
    LiteLLM_MCPServerTable,
    MCPApprovalStatus,
    NewMCPServerRequest,
    UpdateMCPServerRequest,
)
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.client_distribution.storage import (
    create_s3_client,  # pyright: ignore[reportUnknownVariableType]  # optional boto3 stubs
    load_config_from_env,
)
from litellm.proxy.mcp_source_broker.keys import Keys
from litellm.proxy.mcp_source_broker.models import SourceCreate, TransportName
from litellm.proxy.mcp_source_broker.remote_gateway import create_remote_gateway_router
from litellm.proxy.mcp_source_broker.store import Database, NotOwnerConflict, SourceStore
from litellm.proxy.utils import PrismaClient
from litellm.types.mcp import MCPAuth, MCPTransport


class MountableApp(Protocol):
    def include_router(self, router: APIRouter) -> None: ...


ValidatePayload: TypeAlias = Callable[[NewMCPServerRequest], None]


class McpCreate(Protocol):
    async def __call__(
        self, prisma_client: PrismaClient, data: NewMCPServerRequest, touched_by: str
    ) -> LiteLLM_MCPServerTable | McpIdentifierConflict: ...


class S3PutClient(Protocol):
    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> object: ...


class MCPUpdate(Protocol):
    async def __call__(
        self,
        prisma_client: PrismaClient,
        data: UpdateMCPServerRequest,
        touched_by: str,
    ) -> LiteLLM_MCPServerTable | McpIdentifierConflict | None: ...


class ArtifactStore(Protocol):
    async def __call__(self, key: str, body: bytes) -> None: ...


class SubmitServer(Protocol):
    """Submit a server for admin review, owned by the caller's user id.

    This is the BYOM path, not an upsert: submitting an identifier that is
    already registered must re-enter review rather than overwrite the approved
    row in place.
    """

    async def __call__(self, request: NewMCPServerRequest, owner: str) -> None: ...


class CatalogConfig(Protocol):
    def catalog_url(self, path: str) -> str: ...


def create_gateway_router(
    store: SourceStore,
    register: SubmitServer,
    catalog: CatalogConfig,
    service_secret: str,
    *,
    data_plane_url: str = "http://litellm-mcp-broker-dev.adeo-litellm.svc.cluster.local:8888",
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
        if not user.user_id:
            raise HTTPException(400, "enrollment requires an authenticated user")
        if not user.team_id:
            raise HTTPException(400, "enrollment requires a team-scoped key")
        owner: Final = user.user_id
        request: Final = payload.with_owner(owner)
        await store.ensure()
        result: Final = await store.enroll(request, owner)
        if isinstance(result, NotOwnerConflict):
            raise HTTPException(409, "source owner or alias conflicts")
        await put_artifact(result.spec_path, request.spec.encode())
        registration: Final = NewMCPServerRequest(
            server_id=result.source_id,
            server_name=result.alias,
            alias=result.alias,
            url=f"{data_plane_url}/sources/{result.source_id}",
            spec_path=catalog.catalog_url(result.spec_path),
            mcp_access_groups=[f"mcp-source-{result.source_id}"],
            transport=MCPTransport.http,
            auth_type=MCPAuth.bearer_token,
            credentials={"auth_value": service_secret},
        )
        try:
            await register(registration, owner)
        except EnrollmentConflict as conflict:
            raise HTTPException(409, "source identifier is already registered; delete or re-name it") from conflict
        return {"server_id": result.source_id, "status": APPROVAL_PENDING}

    return router


def broker_data_plane_url() -> str:
    return os.environ.get(
        "MCP_SOURCE_BROKER_DATA_PLANE_URL", "http://litellm-mcp-broker-dev.adeo-litellm.svc.cluster.local:8888"
    )


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

    async def create(request: NewMCPServerRequest, owner: str) -> None:
        await submit_for_review(prisma, manager, request, owner)

    async def route_source(source_id: str, transport: TransportName = "reverse-h2") -> None:
        found: Final = await get_mcp_server(prisma, source_id)
        if found is None:
            raise ValueError("source registration is missing")
        await source_store.route(source_id, transport)
        await update_mcp_server(
            prisma,
            UpdateMCPServerRequest(
                server_id=source_id,
                url=f"{broker_data_plane_url()}/sources/{source_id}",
            ),
            touched_by=found.mcp_info.get("owner", "mcp-source-broker") if found.mcp_info else "mcp-source-broker",
        )
        await manager.reload_servers_from_database()

    app.include_router(
        create_gateway_router(source_store, create, _catalog_config(), token, data_plane_url=broker_data_plane_url())
    )
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


async def submit_for_review(
    prisma: PrismaClient,
    manager: MCPServerManager,
    request: NewMCPServerRequest,
    owner: str,
    *,
    create: McpCreate = create_mcp_server_if_identifier_free,
    validate: ValidatePayload = validate_and_normalize_mcp_server_payload,
) -> None:
    """Enter a server into admin review, owned by ``owner``.

    Deliberately not an upsert. Reusing the approve-time reload means a repeat
    enroll of an existing identifier collides instead of silently rewriting an
    approved row back to pending, so every content change re-enters review.
    """
    if not owner:
        raise ValueError("submission requires an owner")
    validate(request)
    request.approval_status = MCPApprovalStatus.pending_review
    request.submitted_by = owner
    request.submitted_at = datetime.now(timezone.utc)
    result: Final = await create(prisma, request, touched_by=owner)
    if isinstance(result, McpIdentifierConflict):
        raise EnrollmentConflict(result)
    await manager.reload_servers_from_database()


class EnrollmentConflict(Exception):
    """The submitted server identifier is already claimed by a live row."""


APPROVAL_PENDING: Final = "pending_review"


def mount_mcp_sources_when_enabled(
    app: MountableApp,
    prisma: PrismaClient,
    manager: MCPServerManager,
    store: SourceStore | None = None,
) -> None:
    if os.environ.get("MCP_SOURCE_BROKER_ENABLED") != "true":
        return
    mount_mcp_sources(app, prisma, manager, store)
