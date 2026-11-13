from __future__ import annotations

import asyncio
import hmac
import os
import shlex
import typing
from collections.abc import Awaitable, Callable
from typing import Annotated, Final, Protocol, TypeAlias

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

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
from litellm.proxy.mcp_source_broker.models import Claim, EnrollResponse, RelayConfig, SourceCreate
from litellm.proxy.mcp_source_broker.store import Database, NotOwner, NotOwnerConflict, SourceStore
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


def create_gateway_router(
    store: SourceStore,
    settings: RelayConfig,
    register: Callable[[NewMCPServerRequest], Awaitable[None]],
    service_secret: str,
    *,
    pool_url: str = "http://litellm-mcp-broker-dev.adeo-litellm.svc.cluster.local:9090",
    broker_url: str = "http://litellm-mcp-broker-dev.adeo-litellm.svc.cluster.local:8089",
    transport: httpx.AsyncBaseTransport | None = None,
    artifacts: Callable[[str, bytes], Awaitable[None]] | None = None,
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
    ) -> EnrollResponse:
        if user.user_role != LitellmUserRoles.PROXY_ADMIN:
            raise HTTPException(403, "proxy administrator required")
        request: Final = payload
        await store.ensure()
        result: Final = await store.enroll(request, request.owner, request.key)
        if isinstance(result, NotOwner):
            raise HTTPException(403)
        if isinstance(result, NotOwnerConflict):
            raise HTTPException(409, "source owner, alias, or key conflicts")
        await put_artifact(result.source.spec_path, request.spec.encode())
        source: Final = result.source
        await register(
            NewMCPServerRequest(
                server_id=source.source_id,
                server_name=source.alias,
                alias=source.alias,
                url=f"{pool_url}/sources/{source.source_id}",
                spec_path=settings.catalog_url(source.spec_path),
                mcp_info={"owner": source.tenant},
                mcp_access_groups=[f"mcp-source-{source.source_id}"],
                transport=MCPTransport.http,
                auth_type=MCPAuth.bearer_token,
                credentials={"auth_value": service_secret},
            )
        )
        return EnrollResponse(
            source_id=result.source.source_id,
            enrollment_id=result.enrollment_id,
            token=result.token,
            install_path=f"/mcp-source-broker/install/{result.enrollment_id}",
        )

    @router.get("/mcp-source-broker/install/{grant_id}")
    async def script(  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        grant_id: str, user: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)], token: str = ""
    ) -> Response:
        owner: Final = await store.owner_of_enrollment(grant_id)
        authenticated: Final = user.user_role == LitellmUserRoles.PROXY_ADMIN or (
            user.user_id is not None and owner is not None and hmac.compare_digest(owner, user.user_id)
        )
        if not token and not authenticated:
            raise HTTPException(404, "installation unavailable")
        headers: Final = {"Authorization": f"Bearer {service_secret}"}
        async with httpx.AsyncClient(base_url=broker_url, transport=transport, timeout=10.0) as client:
            result: Final = (
                await client.post("/v1/claim", json={"enrollment_id": grant_id, "token": token}, headers=headers)
                if token
                else await client.get(f"/v1/claim/{grant_id}", headers=headers)
            )
        if result.status_code != 200:
            raise HTTPException(404, "installation unavailable")
        found: Final = Claim.model_validate_json(result.content)
        return Response(render_installer(found, settings), media_type="text/x-shellscript")

    return router


def render_installer(claim: Claim, config: RelayConfig) -> str:
    user: Final = claim.username
    password: Final = claim.password
    member: Final = claim.member
    target: Final = claim.source.target
    return f"""#!/bin/sh
set -eu
umask 077
state="$HOME/.config/oicm-source-{member.connector_id}"
mkdir -p "$state" "$HOME/.config/systemd/user"
chmod 700 "$state" "$HOME/.config/systemd/user"
archive="$state/chisel.gz"
expected={shlex.quote(config.chisel_sha256)}
curl --proto '=https' --tlsv1.2 --fail --location --output "$archive.new" '{config.public_url}/install/chisel/1.12.0/linux_amd64.gz'
actual=$(sha256sum "$archive.new" | cut -d ' ' -f 1)
[ "$actual" = "$expected" ] || {{ echo 'chisel checksum mismatch' >&2; exit 1; }}
gzip -dc "$archive.new" > "$state/chisel.new"
chmod 500 "$state/chisel.new"
mv -f "$archive.new" "$archive"
mv -f "$state/chisel.new" "$state/chisel"
printf 'CONNECTOR_STATE=%s\nCONNECTOR_AUTH=%s\n' "$state" {shlex.quote(f"{user}:{password}")} > "$state/connector.env"
chmod 600 "$state/connector.env"
cat > "$HOME/.config/systemd/user/mcp-source-connector.service" <<'UNIT'
[Unit]
Description=OICM loopback source connector {claim.source.source_id}

[Service]
Restart=always
RestartSec=2s
EnvironmentFile=%h/.config/oicm-source-{member.connector_id}/connector.env
ExecStart=/bin/sh -c '"${{CONNECTOR_STATE}}/chisel" client {config.relay_url} --fingerprint {config.fingerprint} --auth "${{CONNECTOR_AUTH}}" --keepalive 30s --max-retry-count -1 --max-retry-interval 30s R:{member.relay_host}:{member.relay_port}:{target.host}:{target.port}'
NoNewPrivileges=yes

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now mcp-source-connector.service
"""


def mount_mcp_sources(
    app: MountableApp,
    prisma: PrismaClient,
    manager: MCPServerManager,
    settings: RelayConfig,
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

    app.include_router(create_gateway_router(source_store, settings, create, token))


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
    settings: Final = RelayConfig(
        relay_url=os.environ["MCP_SOURCE_BROKER_RELAY_URL"],
        public_url=os.environ["MCP_SOURCE_BROKER_PUBLIC_URL"],
        fingerprint=os.environ["MCP_SOURCE_BROKER_FINGERPRINT"],
        chisel_sha256=os.environ["MCP_SOURCE_BROKER_CHISEL_SHA256"],
    )
    mount_mcp_sources(app, prisma, manager, settings, store)
