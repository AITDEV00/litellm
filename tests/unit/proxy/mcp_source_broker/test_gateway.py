import shlex

import httpx
import pytest
from respx import MockRouter

from fastapi import FastAPI

from litellm.proxy._experimental.mcp_server.db import _prepare_mcp_server_data
from litellm.proxy._types import LitellmUserRoles, NewMCPServerRequest, UpdateMCPServerRequest
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.mcp_source_broker.control import create_control_app
from litellm.proxy.mcp_source_broker.gateway import (
    create_gateway_router,
    default_registration,
    mount_mcp_sources_when_enabled,
    render_installer,
)
from litellm.proxy.mcp_source_broker.models import RelayConfig, SourceCreate
from litellm.proxy.mcp_source_broker.store import Enrollment, SourceStore


@pytest.mark.asyncio
async def test_gateway_enrolls_and_installs_via_service_auth(
    store: SourceStore,
    source: SourceCreate,
    settings: RelayConfig,
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

    control = create_control_app(store, "service-token", settings)
    app = FastAPI()
    app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(
        user_role=LitellmUserRoles.PROXY_ADMIN, user_id="alice"
    )
    app.include_router(
        create_gateway_router(
            store,
            settings,
            register,
            "service-token",
            transport=httpx.ASGITransport(app=control),
            artifacts=artifact,
            pool_url="http://127.0.0.1:9090",
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        response = await client.post("/mcp-sources", json=source.model_dump(mode="json"))
        assert response.status_code == 201
        result = response.json()
        assert registrations[0].server_id == source.source_id
        assert registrations[0].url == f"http://127.0.0.1:9090/sources/{source.source_id}"
        from litellm.proxy._experimental.mcp_server.openapi_to_mcp_generator import create_tool_function

        call = create_tool_function("/v1/pets", "get", {}, registrations[0].url)
        dispatched = respx_mock.get(f"http://127.0.0.1:9090/sources/{source.source_id}/v1/pets").respond(
            200, json={"source": "fixture"}
        )
        tool_result = await call()
        assert tool_result.value == {"source": "fixture"}
        assert dispatched.calls.last.request.url.path == f"/sources/{source.source_id}/v1/pets"
        assert registrations[0].credentials == {"auth_value": "service-token"}
        assert registrations[0].mcp_access_groups == [f"mcp-source-{source.source_id}"]
        assert uploads[source.spec_path] == source.spec.encode()
        assert registrations[0].spec_path == f"http://127.0.0.1:4000/install/{source.spec_path}"
        installer = await client.get(
            f"/mcp-source-broker/install/{result['enrollment_id']}", params={"token": result["token"]}
        )
        assert installer.status_code == 200 and "systemctl --user" in installer.text
        recovered = await client.get(f"/mcp-source-broker/install/{result['enrollment_id']}")
        assert recovered.text == installer.text
        replay = await client.get(
            f"/mcp-source-broker/install/{result['enrollment_id']}", params={"token": result["token"]}
        )
        assert replay.status_code == 404

        await client.post("/mcp-sources", json=source.model_dump(mode="json"))
        assert len(registrations) == 2
        assert len(store.db.state.sources) == 1
        assert store.db.state.sources[0].tenant == "alice"
        revised_spec = '{"openapi":"3.1.0","paths":{}}'
        store.db.state.sources[0] = store.db.state.sources[0].model_copy(update={"status": "quarantined"})
        repeat_response = await client.post(
            "/mcp-sources", json=source.model_copy(update={"spec": revised_spec}).model_dump(mode="json")
        )
        assert repeat_response.status_code == 201
        assert repeat_response.json()["enrollment_id"] == result["enrollment_id"]
        changed = source.model_copy(update={"spec": revised_spec})
        assert store.db.state.sources[0].expected == changed.fingerprint()
        assert len(store.db.state.members) == 1
        assert store.db.state.sources[0].status == "pending"
        app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(user_id="bob")
        assert (await client.get(f"/mcp-source-broker/install/{result['enrollment_id']}")).status_code == 404
        app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(user_id="alice")
        assert (await client.get(f"/mcp-source-broker/install/{result['enrollment_id']}")).text == installer.text
        app.dependency_overrides[user_api_key_auth] = lambda: UserAPIKeyAuth(
            user_role=LitellmUserRoles.PROXY_ADMIN, user_id="admin"
        )
        assert (await client.get(f"/mcp-source-broker/install/{result['enrollment_id']}")).text == installer.text


@pytest.mark.asyncio
async def test_install_requires_user_authentication(
    store: SourceStore, source: SourceCreate, settings: RelayConfig
) -> None:
    grant = await store.enroll(source, source.owner, source.key)
    claims: list[httpx.Request] = []

    def record_claim(request: httpx.Request) -> httpx.Response:
        claims.append(request)
        return httpx.Response(500)

    async def register(request: NewMCPServerRequest) -> None:
        pass

    app = FastAPI()
    app.include_router(
        create_gateway_router(
            store,
            settings,
            register,
            "service-token",
            transport=httpx.MockTransport(record_claim),
        )
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
        assert (await client.get(f"/mcp-source-broker/install/{grant.enrollment_id}")).status_code == 404
        assert claims == []


def test_relay_urls(settings: RelayConfig) -> None:
    from pydantic import ValidationError

    acceptable = RelayConfig(**{**settings.model_dump(), "public_url": "https://gateway.example/client/"})
    assert acceptable.public_url == "https://gateway.example/client"
    assert acceptable.catalog_url("artifact") == "http://127.0.0.1:4000/install/artifact"
    local_config = RelayConfig(**{**settings.model_dump(), "public_url": "http://localhost:4000"})
    assert local_config.public_url == "http://localhost:4000"
    values = (
        "https://relay.example",
        "https://relay.example/mcp-worker?next=x",
        "https://relay.example/a/../mcp-worker",
    )
    for value in values:
        with pytest.raises(ValidationError, match="URL"):
            RelayConfig(**{**settings.model_dump(), "relay_url": value})


@pytest.mark.asyncio
async def test_installer_pinned_download_checksum_and_persisted_lease(
    store: SourceStore, source: SourceCreate, settings: RelayConfig
) -> None:
    result = await store.enroll(source, source.owner, "key")
    claim = await store.claim(result.enrollment_id, result.token)
    assert claim is not None
    script = render_installer(claim, settings)
    assert script.startswith("#!/bin/sh\nset -eu")
    assert "/install/chisel/1.12.0/linux_amd64.gz" in script
    assert '[ "$actual" = "$expected" ]' in script
    assert "eval" not in script and "--tls-skip-verify" not in script and "sudo" not in script
    assert "mcp-source-connector.service" in script and "$HOME/.config/systemd/user" in script
    assert f"R:{claim.member.relay_host}:{claim.member.relay_port}:127.0.0.1:18080" in script
    exec_line = next(line for line in script.splitlines() if line.startswith("ExecStart="))
    tokens = shlex.split(exec_line.removeprefix("ExecStart="))
    assert tokens[0:2] == ["/bin/sh", "-c"]
    command = shlex.split(tokens[2])
    assert command[0] == "${CONNECTOR_STATE}/chisel" and command[1:3] == ["client", settings.relay_url]
    assert command[command.index("--fingerprint") + 1] == settings.fingerprint
    assert command[command.index("--auth") + 1] == "${CONNECTOR_AUTH}"
    assert "R:" in tokens[2] and "service-token" not in tokens[2]
    assert 'chmod 600 "$state/connector.env"' in script
    assert shlex.quote(f"{claim.username}:{claim.password}") in script
    assert not any(line.startswith("echo") and claim.password in line for line in script.splitlines())


@pytest.mark.asyncio
async def test_registration_inner_factory_registers(source: SourceCreate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITELLM_SALT_KEY", "unit-test-only")
    mcp = NewMCPServerRequest(
        server_id=source.source_id,
        url="http://127.0.0.1:9090/" + source.source_id,
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
    monkeypatch, settings: RelayConfig, store: SourceStore, source: SourceCreate
) -> None:
    async def test_enabled_mount():
        monkeypatch.setenv("MCP_SOURCE_BROKER_ENABLED", "true")
        monkeypatch.setenv("MCP_SOURCE_BROKER_RELAY_URL", settings.relay_url)
        monkeypatch.setenv("MCP_SOURCE_BROKER_PUBLIC_URL", settings.public_url)
        monkeypatch.setenv("MCP_SOURCE_BROKER_FINGERPRINT", settings.fingerprint)
        monkeypatch.setenv("MCP_SOURCE_BROKER_CHISEL_SHA256", settings.chisel_sha256)
        monkeypatch.setenv("MCP_SOURCE_BROKER_PEPPER", "test-only-pepper-at-least-32-characters-long")
        monkeypatch.setenv("MCP_SOURCE_BROKER_SERVICE_TOKEN", "test-only-service-token")
        app = FastAPI()
        mount_mcp_sources_when_enabled(app, object(), object(), store=store)
        routes = {route.path for route in app.routes}
        assert "/mcp-sources" in routes and "/mcp-source-broker/install/{grant_id}" in routes
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway") as client:
            assert (await client.post("/mcp-sources", json=source.model_dump(mode="json"))).status_code == 403

    await test_enabled_mount()
