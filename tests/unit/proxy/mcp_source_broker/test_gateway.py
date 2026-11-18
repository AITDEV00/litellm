from __future__ import annotations

from typing import Final

import httpx
import pytest
from fastapi import FastAPI
from respx import MockRouter

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


def _app(store: SourceStore, register, user: UserAPIKeyAuth, artifacts, resume=None) -> FastAPI:
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
            resume=resume,
        )
    )
    return app


@pytest.mark.asyncio
async def test_ordinary_user_without_team_enrolls(
    store: SourceStore,
    source: SourceCreate,
    respx_mock: MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-service is the point: a key for an admin-created ordinary account with no
    team enrolls its own source, and the owner is the authenticated user. A team gate
    would make this a 400, which is the defect this test pins."""
    monkeypatch.setenv("DISABLE_AIOHTTP_TRANSPORT", "True")
    submissions: list[tuple[NewMCPServerRequest, str]] = []
    uploads: dict[str, bytes] = {}

    async def register(request: NewMCPServerRequest, owner: str) -> None:
        submissions.append((request, owner))

    async def artifact(key: str, body: bytes) -> None:
        uploads[key] = body

    app = _app(store, register, UserAPIKeyAuth(user_id="alice"), artifact)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response: Final = await client.post("/mcp-sources", json=source.model_dump(mode="json"))

    assert response.status_code == 201
    assert response.json() == {"source_id": source.source_id, "owner": "alice", "status": APPROVAL_PENDING}
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
async def test_enroll_requires_a_user(store: SourceStore, source: SourceCreate) -> None:
    async def register(request: NewMCPServerRequest, owner: str) -> None:
        pass

    async def artifact(key: str, body: bytes) -> None:
        pass

    app = _app(store, register, UserAPIKeyAuth(), artifact)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/mcp-sources", json=source.model_dump(mode="json"))
    assert response.status_code == 400, "an unauthenticated caller must not enroll"

    app = _app(store, register, UserAPIKeyAuth(user_id="alice"), artifact)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        accepted = await client.post("/mcp-sources", json=source.model_dump(mode="json"))
    assert accepted.status_code == 201, "a user without a team must be accepted"


@pytest.mark.asyncio
async def test_conflicting_registration_is_a_409(store: SourceStore, source: SourceCreate) -> None:
    """An identifier collision surfaces as a conflict, never as a silent rewrite
    of the existing row."""

    async def register(request: NewMCPServerRequest, owner: str) -> None:
        raise EnrollmentConflict("taken")

    async def artifact(key: str, body: bytes) -> None:
        pass

    app = _app(store, register, UserAPIKeyAuth(user_id="alice"), artifact)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/mcp-sources", json=source.model_dump(mode="json"))
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_repeat_enroll_resumes_its_own_pending_row(store: SourceStore, source: SourceCreate) -> None:
    """A retry after a lost response must not strand the install.

    The first enroll succeeded gateway-side, so the retry collides. When the row is
    the same owner's and still awaiting review there is nothing to protect, so the
    retry reports the same pending enrollment instead of a 409.
    """
    seen: list[tuple[str, str]] = []

    async def register(request: NewMCPServerRequest, owner: str) -> None:
        raise EnrollmentConflict("taken")

    async def resume(*, source_id: str, owner: str) -> bool:
        seen.append((source_id, owner))
        return True

    async def artifact(key: str, body: bytes) -> None:
        pass

    app = _app(store, register, UserAPIKeyAuth(user_id="alice"), artifact, resume)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/mcp-sources", json=source.model_dump(mode="json"))

    assert response.status_code == 201
    assert response.json() == {"source_id": source.source_id, "owner": "alice", "status": APPROVAL_PENDING}
    assert seen == [(source.source_id, "alice")], "resume must be asked about exactly this source and owner"


@pytest.mark.asyncio
async def test_repeat_enroll_on_an_unresumable_row_still_conflicts(store: SourceStore, source: SourceCreate) -> None:
    """Resume is not a general retry: an approved, rejected, or foreign row must
    stay a hard 409 so nothing is moved out of review."""

    async def register(request: NewMCPServerRequest, owner: str) -> None:
        raise EnrollmentConflict("taken")

    async def resume(*, source_id: str, owner: str) -> bool:
        del source_id, owner
        return False

    async def artifact(key: str, body: bytes) -> None:
        pass

    app = _app(store, register, UserAPIKeyAuth(user_id="alice"), artifact, resume)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/mcp-sources", json=source.model_dump(mode="json"))

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_pending_source_resume_rejects_any_row_not_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Prisma-backed resume check is the security boundary, so pin it directly:
    only the same owner's pending_review row is resumable."""
    import litellm.proxy.mcp_source_broker.gateway as gateway_module

    def row(owner: str, approval: str) -> object:
        return type("Server", (), {"submitted_by": owner, "approval_status": approval, "mcp_info": {}})()

    async def decision(found: object | None) -> bool:
        async def lookup(_prisma: object, _source_id: str) -> object | None:
            return found

        monkeypatch.setattr(gateway_module, "get_mcp_server", lookup)
        resume = gateway_module.pending_source_resume(object())  # pyright: ignore[reportArgumentType]  # prisma unused
        return await resume(source_id="sid-1", owner="alice")

    assert await decision(row("alice", "pending_review")) is True
    assert await decision(row("alice", "active")) is False
    assert await decision(row("alice", "rejected")) is False
    assert await decision(row("mallory", "pending_review")) is False
    assert await decision(None) is False



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


@pytest.mark.asyncio
async def test_changed_content_conflict_does_not_upload_over_the_approved_artifact(
    store: SourceStore, source: SourceCreate
) -> None:
    """R5: a re-enroll with different content must not touch the stored artifact.

    Before this, the gateway uploaded the new spec before the registration conflict
    was decided, so an approved source's catalog URL could be swapped by a retry.
    The conflict must leave the upload untouched and never reach submission.
    """
    from litellm.proxy.mcp_source_broker.store import ContentConflict

    uploads: list[tuple[str, bytes]] = []
    submissions: list[NewMCPServerRequest] = []

    async def register(request: NewMCPServerRequest, owner: str) -> None:
        submissions.append(request)

    async def artifact(key: str, body: bytes) -> None:
        uploads.append((key, body))

    async def conflicting_enroll(*args: object, **kwargs: object) -> ContentConflict:
        return ContentConflict(id=source.source_id)

    monkeypatch_target = store
    original = monkeypatch_target.enroll

    async def enroll_with_conflict(value: SourceCreate, owner: str) -> ContentConflict:
        del value, owner
        return ContentConflict(id=source.source_id)

    monkeypatch_target.enroll = enroll_with_conflict  # pyright: ignore[reportAttributeAccessIssue]  # functional test seam
    try:
        app = _app(store, register, UserAPIKeyAuth(user_id="alice"), artifact)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            response = await client.post("/mcp-sources", json=source.model_dump(mode="json"))
    finally:
        monkeypatch_target.enroll = original  # pyright: ignore[reportAttributeAccessIssue]  # functional test seam

    assert response.status_code == 409
    assert "different content" in response.json()["detail"]
    assert uploads == [], "changed content must not overwrite the stored artifact"
    assert submissions == [], "changed content must not be submitted for review"


@pytest.mark.asyncio
async def test_repeat_enroll_creates_no_new_row_upload_or_submission(
    store: SourceStore,
    source: SourceCreate,
    respx_mock: MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N3: an identical re-enroll is idempotent at the gateway, not just the client.

    The first enroll writes the source, uploads its artifact and submits it for
    review. The exact same enroll again must return the stored identity with the
    stored status and must not upload a second artifact, submit a second MCP row,
    open a review, or advance anything. This is enforced inside the store's
    transaction, so it holds even for a concurrent duplicate call.
    """
    monkeypatch.setenv("DISABLE_AIOHTTP_TRANSPORT", "True")
    submissions: list[tuple[NewMCPServerRequest, str]] = []
    uploads: dict[str, bytes] = {}

    async def register(request: NewMCPServerRequest, owner: str) -> None:
        submissions.append((request, owner))

    async def artifact(key: str, body: bytes) -> None:
        uploads[key] = body

    app = _app(store, register, UserAPIKeyAuth(user_id="alice"), artifact)
    payload: Final = source.model_dump(mode="json")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        first: Final = await client.post("/mcp-sources", json=payload)
        second: Final = await client.post("/mcp-sources", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["source_id"] == second.json()["source_id"] == source.source_id
    assert len(submissions) == 1, "a repeat enroll submitted a second MCP row"
    assert len(uploads) == 1, "a repeat enroll uploaded a second artifact"
    assert len(store.db.sources) == 1, "a repeat enroll created a second source"
