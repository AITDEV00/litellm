from __future__ import annotations

from typing import Final

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
async def test_selection_defaults_to_tunnel(store: SourceStore, source: SourceCreate) -> None:
    await store.enroll(source, "alice")
    selection = await store.selection(source.source_id)
    assert selection.transport == "tunnel"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_selection_maps_retired_transport_literals_to_tunnel(store: SourceStore, source: SourceCreate) -> None:
    """Rows enrolled before the transition still store a retired literal; both
    select the tunnel, which is the only plane left, without a data migration."""
    await store.enroll(source, "alice")
    for legacy_name in ("remotedialer", "reverse-h2"):
        legacy: Final = store.db.sources[0].model_copy(update={"remote_transport": legacy_name})
        store.db.sources[0] = legacy
        selection = await store.selection(source.source_id)
        assert selection.transport == "tunnel", legacy_name


@pytest.mark.asyncio
async def test_select_persists_requested_transport(store: SourceStore, source: SourceCreate) -> None:
    from litellm.proxy.mcp_source_broker.remote_gateway import create_remote_gateway_router

    await store.enroll(source, "alice")

    async def route(source_id: str, transport: TransportName = "tunnel") -> None:
        await store.route(source_id, transport)

    app = FastAPI()
    app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(
        user_role=LitellmUserRoles.PROXY_ADMIN, user_id="alice"
    )
    app.include_router(create_remote_gateway_router(store, "service-token", route=route))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        selected = await client.put(
            f"/mcp-source-link-sources/{source.source_id}/selection", json={"transport": "tunnel"}
        )
        assert selected.status_code == 200
        assert (await store.selection(source.source_id)).transport == "tunnel"


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


@pytest.mark.asyncio
async def test_enroll_without_claim_token_still_writes_the_enrollment_anchor(
    store: SourceStore, source: SourceCreate
) -> None:
    """A self-enrolled source has no claim token, but still needs its anchor row.

    "OicmMcpRemoteWorker".enrollment_id is a foreign key onto
    "OicmMcpEnrollment", so skipping the anchor makes every later worker
    registration fail. The expiry is therefore server-derived rather than
    required from the caller.
    """
    tokenless: Final = source.model_copy(update={"expires_at": None})
    result = await store.enroll(tokenless, "alice")
    assert result.source_id == source.source_id
    assert len(store.db.enrollments) == 1
