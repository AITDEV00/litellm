from __future__ import annotations

import hmac
import json
import os
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Annotated, Final, Literal

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response

from litellm.proxy._types import LitellmUserRoles
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.mcp_source_broker.control import service_authenticated
from litellm.proxy.mcp_source_broker.models import (
    LifecycleRequest,
    RemoteGoPeer,
    RemoteHealthReport,
    RemoteLifecycle,
    RemotePeer,
    RemotePeerResponse,
    RemoteRemoteInstall,
    RouteSelection,
)
from litellm.proxy.mcp_source_broker.store import SourceStore


async def administrator(user: Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)]) -> UserAPIKeyAuth:
    if user.user_role != LitellmUserRoles.PROXY_ADMIN:
        raise HTTPException(403, "proxy administrator required")
    return user


def remote_public_install_path(worker_id: str, source_id: str, token: str | None = None) -> str:
    return f"/mcp-remotedialer-sources/{source_id}/workers/{worker_id}/install" + (f"?token={token}" if token else "")


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
        result: Final = await client.post("/v1/remotedialer/revoke", headers=headers)
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
    worker_sha256: str,
    *,
    route: Callable[[str, bool], Awaitable[None]],
    control_url: str = "http://litellm-mcp-broker-dev.adeo-litellm.svc.cluster.local:9091",
    transport: httpx.AsyncBaseTransport | None = None,
) -> APIRouter:
    router: Final = APIRouter()

    @router.get("/mcp-remotedialer-sources/{source_id}/selection", response_model=RouteSelection)
    async def selection(  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        source_id: str, user: Annotated[UserAPIKeyAuth, Depends(administrator)]
    ) -> RouteSelection:
        del user
        return await store.selection(source_id)

    @router.put("/mcp-remotedialer-sources/{source_id}/selection", response_model=RouteSelection)
    async def select(  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        source_id: str, selection: RouteSelection, user: Annotated[UserAPIKeyAuth, Depends(administrator)]
    ) -> RouteSelection:
        del user
        await route(source_id, selection.transport == "remotedialer")
        return selection

    @router.post("/mcp-remotedialer-sources/{source_id}/workers", response_model=RemotePeerResponse, status_code=201)
    async def enroll_peer(  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        source_id: str, peer: RemotePeer, user: Annotated[UserAPIKeyAuth, Depends(administrator)]
    ) -> RemotePeerResponse:
        del user
        created: Final = await store.remote.create_peer(source_id, peer)
        if created is None:
            raise HTTPException(409, "source or worker label is unavailable")
        return created

    @router.get(
        "/mcp-remotedialer-sources/{source_id}/workers/{worker_id}/install",
        response_class=Response,
    )
    async def install(  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        source_id: str,
        worker_id: str,
        token: str | None = None,
    ) -> Response:
        if token is None:
            raise HTTPException(403, "enrollment token required")
        found: Final = await store.remote.worker(worker_id)
        if found is None or found.source_id != source_id:
            raise HTTPException(404, "connector installation unavailable")
        config: Final = await store.remote.config(worker_id, token)
        if config is None:
            raise HTTPException(410, "enrollment token is invalid, expired, or redeemed")
        return Response(render_remote_installer(config, worker_sha256), media_type="text/x-shellscript")

    @router.post(
        "/mcp-remotedialer-sources/{source_id}/workers/{worker_id}/{action}",
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


def render_remote_installer(config: RemoteRemoteInstall, expected_sha256: str) -> str:
    if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256):
        raise ValueError("worker checksum must be SHA-256")
    settings: Final = json.dumps(config.model_dump(), separators=(",", ":"), sort_keys=True)
    unit: Final = f"mcp-remotedialer-{config.worker_id[:8]}.service"
    worker_url: Final = os.environ.get(
        "MCP_REMOTEDIALER_PUBLIC_URL", "https://litellm.ecouncil.ae/mcp-remotedialer-worker"
    )
    download: Final = os.environ.get(
        "MCP_REMOTEDIALER_BINARY_URL",
        "https://litellm.ecouncil.ae/mcp-source-broker/public/install/remotedialer/source-link",
    )
    return f"""#!/bin/sh
set -eu
umask 077
state="$HOME/.config/oicm-remotedialer-{config.worker_id}"
mkdir -p "$state" "$HOME/.config/systemd/user"
chmod 700 "$state" "$HOME/.config/systemd/user"
curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error --output "$state/source-link.new" {shlex.quote(download)}
actual=$(sha256sum "$state/source-link.new" | cut -d ' ' -f 1)
[ "$actual" = {shlex.quote(expected_sha256)} ] || {{ echo 'connector checksum mismatch' >&2; exit 1; }}
chmod 500 "$state/source-link.new"
mv -f "$state/source-link.new" "$state/source-link"
printf %s {shlex.quote(settings)} > "$state/connector.json"
printf 'MCP_REMOTEDIALER_CONNECTOR_CONFIG=%s\\nMCP_REMOTEDIALER_PUBLIC_URL=%s\\n' "$state/connector.json" {shlex.quote(worker_url)} > "$state/connector.env"
chmod 600 "$state/connector.json" "$state/connector.env"
cat > "$HOME/.config/systemd/user/{unit}" <<'UNIT'
[Unit]
Description=OICM remotedialer source {config.worker_id}

[Service]
Restart=always
RestartSec=2s
EnvironmentFile=%h/.config/oicm-remotedialer-{config.worker_id}/connector.env
ExecStart=%h/.config/oicm-remotedialer-{config.worker_id}/source-link connector
NoNewPrivileges=yes

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now {unit}
"""


async def invalidate_local_adapter(worker_id: str, source_id: str) -> None:
    if not worker_id:
        raise HTTPException(400)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9092", timeout=5.0) as client:
        result: Final = await client.post(
            "/v1/remotedialer/revoke",
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

    @app.post("/v1/remotedialer/revoke", status_code=204)
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

    @app.get("/v1/remotedialer/peers", response_model=list[RemoteGoPeer], dependencies=depends)
    async def peers() -> Sequence[RemoteGoPeer]:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        return await store.remote.peers()

    @app.post("/v1/remotedialer/report", dependencies=depends)
    async def report(payload: RemoteHealthReport) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        admitted: Final = await store.remote.report(payload.worker_id, payload.generation, payload.catalog_fingerprint)
        return {"admitted": admitted}

    return app
