import hmac
from collections.abc import Sequence
from typing import Annotated, Final

from fastapi import Depends, FastAPI, Header, HTTPException

from litellm.proxy.mcp_source_broker.models import (
    Claim,
    ClaimRequest,
    Health,
    ProbeRequest,
    ProbeResponse,
    RelayConfig,
    RelayUser,
    SourceRecord,
)
from litellm.proxy.mcp_source_broker.store import NoLease, SourceStore


async def service_authenticated(secret: str, authorization: Annotated[str | None, Header()] = None) -> None:
    if not hmac.compare_digest(authorization or "", f"Bearer {secret}"):
        raise HTTPException(401, "service authentication required")


def create_control_app(store: SourceStore, service_secret: str, config: RelayConfig) -> FastAPI:
    if not service_secret:
        raise ValueError("MCP_SOURCE_BROKER_SERVICE_TOKEN is required")
    app: Final = FastAPI()
    secret: Final = service_secret

    @app.get("/health")
    async def health() -> Health:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        return Health(ok=True)

    from functools import partial

    auth: Final = partial(service_authenticated, secret)

    @app.post("/v1/claim", dependencies=[Depends(auth)])
    async def claim(request: ClaimRequest) -> Claim:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        try:
            result: Final = await store.claim(request.enrollment_id, request.token)
        except NoLease as exc:
            raise HTTPException(503, "relay capacity exhausted") from exc
        if result is None:
            raise HTTPException(410, "invalid, expired, or redeemed enrollment")
        return result

    @app.get("/v1/claim/{grant_id}", dependencies=[Depends(auth)])
    async def recover(grant_id: str) -> Claim:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        result: Final = await store.get_claim_if_user(grant_id)
        if result is None:
            raise HTTPException(404, "connector not found")
        return result

    @app.get("/v1/relay-users", dependencies=[Depends(auth)])
    async def users() -> Sequence[RelayUser]:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        return await store.users()

    @app.get("/v1/config", dependencies=[Depends(auth)])
    async def config_view() -> RelayConfig:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        return config

    @app.get("/v1/sources/{source_id}", dependencies=[Depends(auth)])
    async def status(source_id: str) -> SourceRecord | None:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        result: Final = await store.source(source_id)
        if result is None:
            raise HTTPException(404)
        return result

    @app.post("/v1/probe/{source_id}", dependencies=[Depends(auth)])
    async def probe(source_id: str, request: ProbeRequest) -> ProbeResponse:  # pyright: ignore[reportUnusedFunction]  # registered FastAPI route
        result: Final = await store.probe_connector(
            request.connector_id, source_id, bytes.fromhex(request.credential_digest), request.catalog_fingerprint
        )
        if result is None:
            raise HTTPException(404)
        if result.status != "active":
            raise HTTPException(409, "connector quarantined")
        return ProbeResponse(source_id=source_id, status=result.status)

    return app
