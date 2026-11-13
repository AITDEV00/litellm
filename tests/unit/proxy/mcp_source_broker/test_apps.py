import gzip

import httpx
import pytest

from litellm.proxy.mcp_source_broker.control import create_control_app
from litellm.proxy.mcp_source_broker.pool import PreConnectError, create_pool_app
from litellm.proxy.mcp_source_broker.store import Enrollment, SourceStore


@pytest.mark.asyncio
async def test_claim_service_authentication_and_replay(store: SourceStore, source, settings) -> None:
    grant = await store.enroll(source, source.owner, "catalog-key")
    app = create_control_app(store, "service-token", settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://broker") as client:
        assert (await client.get("/v1/relay-users")).status_code == 401
        response = await client.post(
            "/v1/claim",
            json={"enrollment_id": grant.enrollment_id, "token": grant.token},
            headers={"Authorization": "Bearer service-token"},
        )
        assert response.status_code == 200
        assert "credential_digest" not in response.text
        body = response.json()
        assert body["member"]["relay_host"] == "127.0.0.20" and body["member"]["relay_port"] == 18080
        replay = await client.post(
            "/v1/claim",
            json={"enrollment_id": grant.enrollment_id, "token": grant.token},
            headers={"Authorization": "Bearer service-token"},
        )
        assert replay.status_code == 410


@pytest.mark.asyncio
async def test_pool_forwards_original_request_without_service_credentials(store: SourceStore, source, settings) -> None:
    grant = await store.enroll(source, source.owner, "key")
    claim = await store.claim(grant.enrollment_id, grant.token)
    await store.probe_connector(
        claim.member.connector_id,
        grant.source.source_id,
        store.db.state.members[0].credential_digest,
        grant.source.expected,
    )
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request):
        requests.append(request)

        async def body():
            yield gzip.compress(b"raw-upstream\0123456789")

        return httpx.Response(
            200,
            content=body(),
            headers={"Content-Encoding": "gzip", "x-upstream": "yes", "transfer-encoding": "chunked"},
        )

    app = create_pool_app(store, "service-token", httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pool") as client:
            response = await client.post(
                f"/sources/{source.source_id}/v1/pets?sort=a%20b&sort=d",
                content=b'{"label":1}',
                headers={
                    "Authorization": "Bearer service-token",
                    "X-LiteLLM-Api-Key": "broker-only",
                    "Cookie": "consumer-session=private",
                },
            )
            assert response.status_code == 200 and response.content == b"raw-upstream\0123456789"
    assert len(requests) == 1
    assert str(requests[0].url) == "http://127.0.0.20:18080/v1/pets?sort=a%20b&sort=d"
    assert requests[0].method == "POST" and await requests[0].aread() == b'{"label":1}'
    assert "authorization" not in requests[0].headers and "x-litellm-api-key" not in requests[0].headers
    assert "cookie" not in requests[0].headers


@pytest.mark.asyncio
async def test_pool_retries_one_explicit_presend_alternate_only(store: SourceStore, source) -> None:
    grant = await store.enroll(source, source.owner, "key")
    claim = await store.claim(grant.enrollment_id, grant.token)
    await store.probe_connector(
        claim.member.connector_id,
        grant.source.source_id,
        store.db.state.members[0].credential_digest,
        grant.source.expected,
    )
    healthy = store.db.state.members[0]
    store.db.state.members.append(
        healthy.model_copy(update={"connector_id": "z-alternate", "relay_host": "127.0.0.21"})
    )
    attempts: list[str] = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            attempts.append(str(request.url))
            if len(attempts) == 1:
                raise PreConnectError("unsent", request=request)

            async def body():
                yield b"alternate"

            return httpx.Response(200, request=request, content=body())

    app = create_pool_app(store, "service-token", Transport())
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pool") as client:
            response = await client.get(
                f"/sources/{source.source_id}/pets", headers={"Authorization": "Bearer service-token"}
            )
            assert response.status_code == 200 and response.content == b"alternate"
            missing = await client.get("/sources/not-a-source/pets", headers={"Authorization": "Bearer service-token"})
            assert missing.status_code == 503
    assert [attempt.split("/")[2] for attempt in attempts] == ["127.0.0.20:18080", "127.0.0.21:18080"]


@pytest.mark.asyncio
async def test_connect_error_never_retries_and_timeout_504(store: SourceStore, source) -> None:
    grant = await store.enroll(source, source.owner, "key")
    claim = await store.claim(grant.enrollment_id, grant.token)
    await store.probe_connector(
        claim.member.connector_id,
        grant.source.source_id,
        store.db.state.members[0].credential_digest,
        grant.source.expected,
    )
    attempts = []

    def upstream(request: httpx.Request):
        attempts.append(request)
        raise httpx.ConnectError("could be after bytes", request=request)

    app = create_pool_app(store, "service-token", httpx.MockTransport(upstream))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pool") as client:
            response = await client.get(
                f"/sources/{source.source_id}/", headers={"Authorization": "Bearer service-token"}
            )
            assert response.status_code == 503 and len(attempts) == 1

    async def timeout(request: httpx.Request):
        raise httpx.ReadTimeout("wait", request=request)

    app = create_pool_app(store, "service-token", httpx.MockTransport(timeout))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://pool") as client:
            assert (
                await client.get(f"/sources/{source.source_id}/", headers={"Authorization": "Bearer service-token"})
            ).status_code == 504
