from __future__ import annotations

from typing import Final

import httpx
import pytest
from respx import MockRouter

from fastapi import FastAPI

from litellm.proxy._experimental.mcp_server.db import _prepare_mcp_server_data
from litellm.proxy._types import LitellmUserRoles, NewMCPServerRequest, UpdateMCPServerRequest
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.mcp_source_broker.gateway import (
    create_gateway_router,
    default_registration,
    mount_mcp_sources_when_enabled,
)
from litellm.proxy.mcp_source_broker.models import SourceCreate
from litellm.proxy.mcp_source_broker.store import SourceStore


@pytest.mark.asyncio
async def test_gateway_enrolls_catalog_and_registers_mcp_row(
    store: SourceStore,
    source: SourceCreate,
    respx_mock: MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISABLE_AIOHTTP_TRANSPORT", "True")
    registrations: list[NewMCPServerRequest] = []
    uploads: dict[str, bytes] = {}

    async def register(request: NewMCPServerRequest) -> None:
        registrations.append(request)

    async def artifact(key: str, body: bytes) -> None:
        uploads[key] = body

    class Catalog:
        def catalog_url(self, path: str) -> str:
            return f"http://127.0.0.1:4000/install/{path}"

    app = FastAPI()
    app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(
        user_role=LitellmUserRoles.PROXY_ADMIN, user_id="alice"
    )
    app.include_router(
        create_gateway_router(
            store,
            register,
            Catalog(),
            "service-token",
            data_plane_url="http://127.0.0.1:9092",
            artifacts=artifact,
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response: Final = await client.post("/mcp-sources", json=source.model_dump(mode="json"))
        assert response.status_code == 201
        assert registrations[0].server_id == source.source_id
        assert registrations[0].url == f"http://127.0.0.1:9092/sources/{source.source_id}"
        from litellm.proxy._experimental.mcp_server.openapi_to_mcp_generator import create_tool_function

        call = create_tool_function("/v1/pets", "get", {}, registrations[0].url)
        dispatched = respx_mock.get(f"http://127.0.0.1:9092/sources/{source.source_id}/v1/pets").respond(
            200, json={"source": "fixture"}
        )
        tool_result = await call()
        assert tool_result.value == {"source": "fixture"}
        assert dispatched.calls.last.request.url.path == f"/sources/{source.source_id}/v1/pets"
        assert registrations[0].credentials == {"auth_value": "service-token"}
        assert registrations[0].mcp_access_groups == [f"mcp-source-{source.source_id}"]
        assert uploads[source.spec_path] == source.spec.encode()
        assert registrations[0].spec_path == f"http://127.0.0.1:4000/install/{source.spec_path}"


@pytest.mark.asyncio
async def test_enroll_requires_admin(store: SourceStore, source: SourceCreate) -> None:
    class Catalog:
        def catalog_url(self, path: str) -> str:
            return f"http://127.0.0.1:4000/install/{path}"

    async def register(request: NewMCPServerRequest) -> None:
        pass

    app = FastAPI()
    app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(user_id="mallory")
    app.include_router(create_gateway_router(store, register, Catalog(), "service-token"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/mcp-sources", json=source.model_dump(mode="json"))
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_registration_inner_factory_registers(source: SourceCreate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITELLM_SALT_KEY", "unit-test-only")
    mcp = NewMCPServerRequest(
        server_id=source.source_id,
        url="http://127.0.0.1:9092/" + source.source_id,
        spec_path="/install/catalog",
        mcp_info={"owner": "alice"},
    )
    calls: list[NewMCPServerRequest] = []

    async def read(_: object, server_id: str) -> None:
        assert server_id == source.source_id

    async def create(_: object, request: NewMCPServerRequest, touched_by: str) -> object:
        assert touched_by == "alice" and request == mcp
        return object()

    class Manager:
        async def reload_servers_from_database(self) -> None:
            calls.append(mcp)

    await default_registration(object(), Manager(), mcp, find=read, create=create)
    assert calls == [mcp]

    async def existing(_: object, server_id: str) -> object:
        return object()

    async def update(
        _: object, value: UpdateMCPServerRequest, touched_by: str, fields_set: set[str] | None = None
    ) -> object:
        assert touched_by == "alice"
        assert fields_set is None
        assert value.mcp_info == {"owner": "alice"}
        prepared = _prepare_mcp_server_data(value, exclude_unset=True)
        assert "extra_headers" not in prepared
        assert "env_vars" not in prepared
        assert "static_headers" not in prepared
        assert all(value is not None for value in prepared.values())
        return object()

    await default_registration(object(), Manager(), mcp, find=existing, create=create, update=update)
    assert calls == [mcp, mcp]


@pytest.mark.asyncio
async def test_mount_after_prisma_initialization(
    monkeypatch, store: SourceStore, source: SourceCreate
) -> None:
    async def test_enabled_mount():
        monkeypatch.setenv("MCP_SOURCE_BROKER_ENABLED", "true")
        monkeypatch.setenv("MCP_SOURCE_BROKER_PEPPER", "test-only-pepper-at-least-32-characters-long")
        monkeypatch.setenv("MCP_SOURCE_BROKER_SERVICE_TOKEN", "test-only-service-token")
        app = FastAPI()
        mount_mcp_sources_when_enabled(app, object(), object(), store=store)
        routes = {route.path for route in app.routes}
        assert "/mcp-sources" in routes
        assert "/mcp-remotedialer-sources/{source_id}/workers" in routes
        assert all("install" not in path for path in routes), "installer routes must be gone"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            assert (await client.post("/mcp-sources", json=source.model_dump(mode="json"))).status_code == 403

    await test_enabled_mount()
