from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import httpx
import pytest

from litellm.proxy.mcp_source_broker.models import SourceCreate
from litellm.proxy.mcp_source_broker.probes import probe_once
from litellm.proxy.mcp_source_broker.store import SourceStore


@pytest.mark.asyncio
async def test_pending_source_probes_activate_then_mismatch_quarantines(
    store: SourceStore, source: SourceCreate
) -> None:
    enrolled: Final = await store.enroll(source, source.owner, "key")
    claim: Final = await store.claim(enrolled.enrollment_id, enrolled.token)
    assert claim is not None
    users: Final = await store.users()
    assert [(user.status, user.enrollment_id) for user in users] == [("pending", enrolled.enrollment_id)]
    documents: Final = [source.spec]
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=documents[-1].encode())

    transport: Final = httpx.MockTransport(upstream)
    await probe_once(store, transport)
    member: Final = store.db.state.members[0]
    actual_digest: Final = member.credential_digest.hex()
    assert member.status == "active" and member.catalog_fingerprint == source.fingerprint()
    assert member.healthy_at == store.db.now
    assert [str(request.url) for request in requests] == ["http://127.0.0.20:18080/openapi.json"]
    assert actual_digest == store.keyring.digest(enrolled.enrollment_id, claim.member.connector_id).hex()
    assert actual_digest != __import__("hashlib").sha256(claim.password.encode()).hexdigest()

    documents.append('{"openapi":"3.1.0","paths":{"/v1/other":{}}}')
    await probe_once(store, transport)
    assert store.db.state.members[0].status == "quarantine"
    assert store.db.state.sources[0].status == "quarantined"
    assert [(user.connector_id, user.status) for user in await store.users()] == [
        (claim.member.connector_id, "quarantine")
    ]

    documents.append(source.spec)
    await probe_once(store, transport)
    assert store.db.state.members[0].status == "active"
    assert store.db.state.sources[0].status == "active"


@pytest.mark.asyncio
async def test_unreachable_refreshes_stale_health(store: SourceStore, source: SourceCreate) -> None:
    enrolled: Final = await store.enroll(source, source.owner, "key")
    claim: Final = await store.claim(enrolled.enrollment_id, enrolled.token)
    assert claim is not None

    await probe_once(store, httpx.MockTransport(lambda request: httpx.Response(200, content=source.spec.encode())))
    assert store.db.state.members[0].status == "active"

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    await probe_once(store, httpx.MockTransport(unavailable))
    assert store.db.state.members[0].healthy_at is None
    assert store.db.state.members[0].status == "quarantine"
    assert store.db.state.sources[0].status == "active"
    recovered = await store.get_claim_if_user(enrolled.enrollment_id)
    assert recovered is not None and recovered.member.status == "quarantine"
    assert [(user.connector_id, user.status) for user in await store.users()] == [
        (claim.member.connector_id, "quarantine")
    ]

    await probe_once(store, httpx.MockTransport(lambda request: httpx.Response(200, content=source.spec.encode())))
    assert store.db.state.members[0].status == "active"
    assert store.db.state.members[0].healthy_at is not None
    assert store.db.state.sources[0].status == "active"
