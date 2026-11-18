from __future__ import annotations

from typing import Final

import httpx
import pytest
from respx import MockRouter

from fastapi import FastAPI

from litellm.proxy._types import MCPApprovalStatus, NewMCPServerRequest
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.mcp_source_broker.gateway import (
    APPROVAL_PENDING,
    EnrollmentConflict,
    create_gateway_router,
    mount_mcp_sources_when_enabled,
    submit_for_review,
)
from litellm.proxy.mcp_source_broker.models import SourceCreate
from litellm.proxy.mcp_source_broker.store import SourceStore


class Catalog:
    def catalog_url(self, path: str) -> str:
        return f"http://127.0.0.1:4000/install/{path}"


def _app(store: SourceStore, register, user: UserAPIKeyAuth, artifacts) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[user_api_key_auth] = lambda: user
    app.include_router(
        create_gateway_router(
            store,
            register,
            Catalog(),
            "service-token",
            data_plane_url="http://127.0.0.1:9092",
            artifacts=artifacts,
        )
    )
    return app


@pytest.mark.asyncio
async def test_team_member_enrolls_without_admin(
    store: SourceStore,
    source: SourceCreate,
    respx_mock: MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-service is the point: a team-scoped key enrolls its own source and
    the owner is the authenticated user, not a claim. The old admin-only gate
    would make this a 403."""
    monkeypatch.setenv("DISABLE_AIOHTTP_TRANSPORT", "True")
    submissions: list[tuple[NewMCPServerRequest, str]] = []
    uploads: dict[str, bytes] = {}

    async def register(request: NewMCPServerRequest, owner: str) -> None:
        submissions.append((request, owner))

    async def artifact(key: str, body: bytes) -> None:
        uploads[key] = body

    app = _app(store, register, UserAPIKeyAuth(user_id="alice", team_id="team-a"), artifact)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response: Final = await client.post("/mcp-sources", json=source.model_dump(mode="json"))

    assert response.status_code == 201
    assert response.json() == {"server_id": source.source_id, "status": APPROVAL_PENDING}
    request, owner = submissions[0]
    assert owner == "alice"
    assert request.server_id == source.source_id
    assert request.url == f"http://127.0.0.1:9092/sources/{source.source_id}"
    assert request.credentials == {"auth_value": "service-token"}
    assert request.mcp_access_groups == [f"mcp-source-{source.source_id}"]
    # Ownership lives in submitted_by; there must be no second copy.
    assert request.mcp_info is None or "owner" not in request.mcp_info
    assert uploads[source.spec_path] == source.spec.encode()

    from litellm.proxy._experimental.mcp_server.openapi_to_mcp_generator import create_tool_function

    call = create_tool_function("/v1/pets", "get", {}, request.url)
    dispatched = respx_mock.get(f"http://127.0.0.1:9092/sources/{source.source_id}/v1/pets").respond(
        200, json={"source": "fixture"}
    )
    assert (await call()).value == {"source": "fixture"}
    assert dispatched.calls.last.request.url.path == f"/sources/{source.source_id}/v1/pets"


@pytest.mark.asyncio
async def test_enroll_requires_a_user_and_a_team(store: SourceStore, source: SourceCreate) -> None:
    async def register(request: NewMCPServerRequest, owner: str) -> None:
        pass

    async def artifact(key: str, body: bytes) -> None:
        pass

    for user in (UserAPIKeyAuth(), UserAPIKeyAuth(user_id="alice")):
        app = _app(store, register, user, artifact)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            response = await client.post("/mcp-sources", json=source.model_dump(mode="json"))
        assert response.status_code == 400, user


@pytest.mark.asyncio
async def test_conflicting_registration_is_a_409(store: SourceStore, source: SourceCreate) -> None:
    """An identifier collision surfaces as a conflict, never as a silent rewrite
    of the existing row."""

    async def register(request: NewMCPServerRequest, owner: str) -> None:
        raise EnrollmentConflict("taken")

    async def artifact(key: str, body: bytes) -> None:
        pass

    app = _app(store, register, UserAPIKeyAuth(user_id="alice", team_id="team-a"), artifact)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/mcp-sources", json=source.model_dump(mode="json"))
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_submit_for_review_stamps_pending_owner_and_never_upserts() -> None:
    """The submission path must create, not update. Routing through the upsert
    would let a repeat enroll silently reactivate an approved or rejected row."""
    request = NewMCPServerRequest(server_id="source-1", server_name="source-1", url="http://127.0.0.1:9092/x")
    created: list[NewMCPServerRequest] = []
    reloads: list[bool] = []

    async def create(_: object, payload: NewMCPServerRequest, touched_by: str) -> object:
        assert touched_by == "alice"
        created.append(payload)
        return object()

    class Manager:
        async def reload_servers_from_database(self) -> None:
            reloads.append(True)

    await submit_for_review(object(), Manager(), request, "alice", create=create, validate=lambda _: None)  # type: ignore[arg-type]

    assert len(created) == 1, "submission must create, not update, exactly once"
    assert request.approval_status == MCPApprovalStatus.pending_review
    assert request.submitted_by == "alice"
    assert request.submitted_at is not None
    assert reloads == [True]


@pytest.mark.asyncio
async def test_submit_for_review_raises_on_identifier_conflict() -> None:
    from litellm.proxy._experimental.mcp_server.db import McpIdentifierConflict

    request = NewMCPServerRequest(server_id="source-1", server_name="source-1", url="http://127.0.0.1:9092/x")

    async def create(_: object, payload: NewMCPServerRequest, touched_by: str) -> object:
        return McpIdentifierConflict(field="server_name", value="source-1", server_id="existing-row")

    class Manager:
        async def reload_servers_from_database(self) -> None:
            raise AssertionError("a conflict must not reload the registry")

    with pytest.raises(EnrollmentConflict):
        await submit_for_review(object(), Manager(), request, "alice", create=create, validate=lambda _: None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_mount_after_prisma_initialization(monkeypatch, store: SourceStore, source: SourceCreate) -> None:
    monkeypatch.setenv("MCP_SOURCE_BROKER_ENABLED", "true")
    monkeypatch.setenv("MCP_SOURCE_BROKER_PEPPER", "test-only-pepper-at-least-32-characters-long")
    monkeypatch.setenv("MCP_SOURCE_BROKER_SERVICE_TOKEN", "test-only-service-token")
    app = FastAPI()
    mount_mcp_sources_when_enabled(app, object(), object(), store=store)
    routes = {route.path for route in app.routes}
    assert "/mcp-sources" in routes
    assert "/mcp-source-link-sources/{source_id}/workers" in routes
    assert all("install" not in path for path in routes), "installer routes must be gone"
