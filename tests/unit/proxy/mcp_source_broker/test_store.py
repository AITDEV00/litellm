from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from litellm.proxy._types import LitellmUserRoles
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.mcp_source_broker.models import SourceCreate, TransportName
from litellm.proxy.mcp_source_broker.store import PermissionDenied, SourceStore


@pytest.mark.asyncio
async def test_enroll_creates_source_and_enrollment_anchor(store: SourceStore, source: SourceCreate) -> None:
    result = await store.enroll(source, "alice")
    assert result.source_id == source.source_id
    assert result.status == "pending"
    assert result.expected == source.fingerprint()
    assert store.db.sources[0].tenant == "alice"
    assert store.db.timeouts == [10000]
    assert store.db.enrollments == [store.keyring.enrollment_id(source.source_id)]


@pytest.mark.asyncio
async def test_enroll_rejects_owner_mismatch(store: SourceStore, source: SourceCreate) -> None:
    with pytest.raises(PermissionDenied):
        await store.enroll(source, "bob")


@pytest.mark.asyncio
async def test_reenroll_same_catalog_is_idempotent(store: SourceStore, source: SourceCreate) -> None:
    first = await store.enroll(source, "alice")
    second = await store.enroll(source, "alice")
    assert first.source_id == second.source_id
    assert len(store.db.enrollments) == 1


@pytest.mark.asyncio
async def test_reenroll_changed_catalog_resets_to_pending(store: SourceStore, source: SourceCreate) -> None:
    await store.enroll(source, "alice")
    revised: SourceCreate = source.model_copy(update={"spec": '{"openapi":"3.1.0","paths":{}}'})
    result = await store.enroll(revised, "alice")
    assert result.expected == revised.fingerprint()
    assert result.status == "pending"
    assert len(store.db.sources) == 1


@pytest.mark.asyncio
async def test_selection_reports_remotedialer(store: SourceStore, source: SourceCreate) -> None:
    await store.enroll(source, "alice")
    selection = await store.selection(source.source_id)
    assert selection.transport == "remotedialer"


@pytest.mark.asyncio
async def test_select_persists_requested_transport(store: SourceStore, source: SourceCreate) -> None:
    from litellm.proxy.mcp_source_broker.remote_gateway import create_remote_gateway_router

    await store.enroll(source, "alice")

    async def route(source_id: str, transport: TransportName = "remotedialer") -> None:
        await store.route(source_id, transport)

    app = FastAPI()
    app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(
        user_role=LitellmUserRoles.PROXY_ADMIN, user_id="alice"
    )
    app.include_router(create_remote_gateway_router(store, "service-token", route=route))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        selected = await client.put(
            f"/mcp-remotedialer-sources/{source.source_id}/selection", json={"transport": "reverse-h2"}
        )
        assert selected.status_code == 200
        assert (await store.selection(source.source_id)).transport == "reverse-h2"


@pytest.mark.asyncio
async def test_reenroll_same_catalog_changes_target(store: SourceStore, source: SourceCreate) -> None:
    await store.enroll(source, "alice")
    revised = source.model_copy(update={"target": source.target.model_copy(update={"port": 18082})})
    result = await store.enroll(revised, "alice")
    assert result.target.port == 18082
    stored = await store.source(source.source_id)
    assert stored is not None and stored.target.port == 18082


@pytest.mark.asyncio
async def test_selection_missing_source_raises_keyerror(store: SourceStore) -> None:
    with pytest.raises(KeyError):
        await store.selection("missing-source-id")
