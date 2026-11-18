from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Final

import httpx
import pytest
from fastapi import FastAPI

from litellm.proxy._types import LitellmUserRoles
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.mcp_source_broker.models import RemotePeer, RemotePeerResponse, Target
from litellm.proxy.mcp_source_broker.remote_gateway import create_remote_gateway_router
from litellm.proxy.mcp_source_broker.store import SourceStore


class IssueRemote:
    """Functional seam for the remote store: issues a deterministic identity.

    Mirrors ``RemoteStore.create_peer`` exactly where the route cares: it rejects a
    duplicate label with ``None`` and otherwise returns the full issued identity.
    """

    def __init__(self) -> None:
        self.labels: Final[dict[str, str]] = {}
        self.calls: Final[list[tuple[str, RemotePeer]]] = []
        self.resumes: Final[list[tuple[str, str, str, int]]] = []
        self.resumable: bool = False

    async def create_peer(self, source_id: str, peer: RemotePeer) -> RemotePeerResponse | None:
        self.calls.append((source_id, peer))
        key: Final = f"{source_id}:{peer.label}"
        if key in self.labels:
            return None
        self.labels[key] = peer.label
        return RemotePeerResponse(
            worker_id=f"worker-{peer.label}",
            source_id=source_id,
            target=f"{peer.target.host}:{peer.target.port}",
            generation=1,
            credential="ab" * 32,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            label=peer.label,
            dataset_id=peer.dataset_id,
        )

    async def resume_peer(self, source_id: str, peer: RemotePeer) -> RemotePeerResponse | None:
        self.resumes.append((source_id, peer.label, peer.target.host, peer.target.port))
        if not self.resumable:
            return None
        return RemotePeerResponse(
            worker_id=f"worker-{peer.label}",
            source_id=source_id,
            target=f"{peer.target.host}:{peer.target.port}",
            generation=1,
            credential="ab" * 32,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            label=peer.label,
            dataset_id=peer.dataset_id,
        )


def _app(store: SourceStore, remote: IssueRemote, user: UserAPIKeyAuth) -> FastAPI:
    app = FastAPI()
    app.dependency_overrides[user_api_key_auth] = lambda: user
    store.remote = remote  # pyright: ignore[reportAttributeAccessIssue]  # functional test seam
    app.include_router(create_remote_gateway_router(store, "service-secret", route=lambda _sid, _transport: _noop()))
    return app


async def _noop() -> None:
    return None


def _peer() -> RemotePeer:
    return RemotePeer(
        label="conn-a",
        dataset_id="dataset-1",
        proof_key="A" * 43 + "=",
        target=Target(),
    )


@pytest.mark.asyncio
async def test_owner_bootstraps_own_source_and_receives_identity(store: SourceStore, source) -> None:
    """The ordinary owner key issues a real identity: a generation, a target, and a
    per-worker credential. Nothing admin-secret is present."""
    store.db.sources.append(
        type(
            "Row",
            (),
            {
                "source_id": "sid-1",
                "tenant": "alice",
                "status": "active",
                "remote_transport": "tunnel",
                "model_dump": lambda self: {
                    "source_id": "sid-1",
                    "tenant": "alice",
                    "key": "k",
                    "alias": "pets_api",
                    "target_host": "127.0.0.1",
                    "target_port": 18080,
                    "spec_path": "p",
                    "expected": "e",
                    "gateway_auth_ciphertext": "c",
                    "status": "active",
                    "revision": 1,
                    "remote_transport": "tunnel",
                },
            },
        )()
    )
    remote: Final = IssueRemote()
    app = _app(store, remote, UserAPIKeyAuth(user_id="alice"))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        response = await client.post("/mcp-source-link-sources/sid-1/workers", json=_peer().model_dump())

    assert response.status_code == 201
    body: Final = response.json()
    assert body["worker_id"] == "worker-conn-a"
    assert body["generation"] == 1
    assert body["target"] == "127.0.0.1:18080"
    assert len(body["credential"]) == 64
    leaked: Final = {"pepper", "service_token", "token_hash", "credential_digest", "gateway_auth_ciphertext"}
    assert leaked.isdisjoint(body), f"response leaked admin state: {sorted(leaked & body.keys())}"


@pytest.mark.asyncio
async def test_other_user_cannot_bootstrap_a_source_they_do_not_own(store: SourceStore, source) -> None:
    store.db.sources.append(
        type(
            "Row",
            (),
            {
                "source_id": "sid-1",
                "tenant": "alice",
                "status": "active",
                "remote_transport": "tunnel",
                "model_dump": lambda self: {
                    "source_id": "sid-1",
                    "tenant": "alice",
                    "key": "k",
                    "alias": "pets_api",
                    "target_host": "127.0.0.1",
                    "target_port": 18080,
                    "spec_path": "p",
                    "expected": "e",
                    "gateway_auth_ciphertext": "c",
                    "status": "active",
                    "revision": 1,
                    "remote_transport": "tunnel",
                },
            },
        )()
    )
    remote: Final = IssueRemote()
    app = _app(store, remote, UserAPIKeyAuth(user_id="mallory"))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        response = await client.post("/mcp-source-link-sources/sid-1/workers", json=_peer().model_dump())

    assert response.status_code == 403, "a non-owner must not bootstrap another user's source"
    assert remote.calls == [], "the store must never be reached for an unauthorized caller"


@pytest.mark.asyncio
async def test_unknown_source_is_forbidden_not_a_500(store: SourceStore, source) -> None:
    app = _app(store, IssueRemote(), UserAPIKeyAuth(user_id="alice"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        response = await client.post("/mcp-source-link-sources/missing/workers", json=_peer().model_dump())
    assert response.status_code == 403, "an unknown source must not be distinguishable from a foreign one"


@pytest.mark.asyncio
async def test_proxy_admin_may_bootstrap_any_source(store: SourceStore, source) -> None:
    remote: Final = IssueRemote()
    app = _app(store, remote, UserAPIKeyAuth(user_id="root", user_role=LitellmUserRoles.PROXY_ADMIN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        response = await client.post("/mcp-source-link-sources/anything/workers", json=_peer().model_dump())
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_duplicate_label_is_a_conflict(store: SourceStore, source) -> None:
    store.db.sources.append(
        type(
            "Row",
            (),
            {
                "source_id": "sid-1",
                "tenant": "alice",
                "status": "active",
                "remote_transport": "tunnel",
                "model_dump": lambda self: {
                    "source_id": "sid-1",
                    "tenant": "alice",
                    "key": "k",
                    "alias": "pets_api",
                    "target_host": "127.0.0.1",
                    "target_port": 18080,
                    "spec_path": "p",
                    "expected": "e",
                    "gateway_auth_ciphertext": "c",
                    "status": "active",
                    "revision": 1,
                    "remote_transport": "tunnel",
                },
            },
        )()
    )
    remote: Final = IssueRemote()
    app = _app(store, remote, UserAPIKeyAuth(user_id="alice"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        first = await client.post("/mcp-source-link-sources/sid-1/workers", json=_peer().model_dump())
        second = await client.post("/mcp-source-link-sources/sid-1/workers", json=_peer().model_dump())
    assert first.status_code == 201
    assert second.status_code == 409, "a duplicate label must not silently reissue the active identity"


@pytest.mark.asyncio
async def test_lost_response_resumes_the_same_worker_identity(store: SourceStore, source) -> None:
    """A duplicate label is only a conflict when there is nothing safe to resume.

    The B1 case: the first bootstrap created the worker row but the response was
    lost, so the connector has no credential. The same owner asking again must get
    that same worker's credential at the same generation, not a 409 and not a new
    row.
    """
    store.db.sources.append(
        type(
            "Row",
            (),
            {
                "source_id": "sid-1",
                "tenant": "alice",
                "status": "active",
                "remote_transport": "tunnel",
                "model_dump": lambda self: {
                    "source_id": "sid-1",
                    "tenant": "alice",
                    "key": "k",
                    "alias": "pets_api",
                    "target_host": "127.0.0.1",
                    "target_port": 18080,
                    "spec_path": "p",
                    "expected": "e",
                    "gateway_auth_ciphertext": "c",
                    "status": "active",
                    "revision": 1,
                    "remote_transport": "tunnel",
                },
            },
        )()
    )
    remote: Final = IssueRemote()
    remote.resumable = True
    app = _app(store, remote, UserAPIKeyAuth(user_id="alice"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw") as client:
        first = await client.post("/mcp-source-link-sources/sid-1/workers", json=_peer().model_dump())
        resumed = await client.post("/mcp-source-link-sources/sid-1/workers", json=_peer().model_dump())

    assert first.status_code == 201
    assert resumed.status_code == 201, "a lost response must be recoverable by the same owner"
    first_body: Final = first.json()
    resumed_body: Final = resumed.json()
    assert resumed_body["worker_id"] == first_body["worker_id"], "resume must return the existing worker, not a new one"
    assert resumed_body["generation"] == first_body["generation"], "resume must not advance the generation"
    assert resumed_body["credential"] == first_body["credential"], "the re-derived credential must be the same one"
    assert remote.resumes == [("sid-1", "conn-a", "127.0.0.1", 18080)]
