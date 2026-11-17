from __future__ import annotations

import hashlib
import typing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import TracebackType
from typing import Final

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from litellm.proxy.mcp_source_broker.keys import Keys
from litellm.proxy.mcp_source_broker.models import (
    RemotePeer,
    SourceRecord,
    Target,
)
from litellm.proxy.mcp_source_broker.remote_store import RemoteStore, RemoteWorkerRow
from litellm.proxy.mcp_source_broker.store import Database

PEPPER: Final = "p" * 32


class FakeTx:
    def __init__(self, database: RemoteMemoryDatabase) -> None:
        self.database: Final = database

    async def query_raw(self, sql: str, *args: object) -> object:
        return await self.database.query_raw(sql, *args)

    async def execute_raw(self, sql: str, *args: object) -> int:
        return await self.database.execute_raw(sql, *args)

    async def __aenter__(self) -> FakeTx:
        return self

    async def __aexit__(self, kind: object, error: object, tb: TracebackType | None) -> None:
        return None


@dataclass
class RemoteMemoryDatabase:
    workers: list[RemoteWorkerRow] = field(default_factory=list)
    sources: list[SourceRecord] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    timeouts: list[int] = field(default_factory=list)

    def tx(self, *, timeout: int = 5000) -> FakeTx:
        self.timeouts.append(timeout)
        return FakeTx(self)

    @staticmethod
    def _hex(value: object) -> bytes:
        assert isinstance(value, str) and value.startswith("\\x"), "expected bytea literal"
        return bytes.fromhex(value[2:])

    @staticmethod
    def _int(value: object) -> int:
        assert isinstance(value, int)
        return value

    def _worker_by_id(self, worker_id: str) -> RemoteWorkerRow | None:
        return next((w for w in self.workers if w.worker_id == worker_id), None)

    async def query_raw(self, sql: str, *args: object) -> list[dict[str, object]]:
        if "SELECT pg_advisory_xact_lock" in sql:
            return []
        if "WHERE worker_id=$1" in sql and "FROM" in sql:
            found: Final = self._worker_by_id(str(args[0]))
            return [found.model_dump()] if found else []
        if "row_to_json(w) AS worker" in sql:
            rows: list[dict[str, object]] = []
            for worker in self.workers:
                source = next((s for s in self.sources if s.source_id == worker.source_id), None)
                if source is None:
                    continue
                rows.append({"worker": worker.model_dump(), "expected": source.expected})
            return rows
        if "SELECT worker_id FROM" in sql:
            return [{"worker_id": w.worker_id} for w in self.workers if w.source_id == args[0] and w.label == args[1]]
        raise AssertionError(sql[:120])

    async def execute_raw(self, sql: str, *args: object) -> int:
        if "CREATE TABLE" in sql or "ADD COLUMN" in sql or "pg_advisory_xact_lock" in sql:
            return 0
        if "SET generation=$2,status=$3" in sql:
            worker = self._worker_by_id(str(args[0]))
            if worker is not None:
                self._replace(
                    worker,
                    worker.model_copy(update={"generation": self._int(args[1]), "status": str(args[2])}),
                )
                return 1
            return 0
        if "SET healthy_at=NULL,catalog_fingerprint=NULL,token_hash=$2::bytea" in sql:
            worker = self._worker_by_id(str(args[0]))
            if worker is not None:
                expires = args[2]
                assert isinstance(expires, datetime)
                self._replace(
                    worker,
                    worker.model_copy(
                        update={
                            "healthy_at": None,
                            "catalog_fingerprint": None,
                            "token_hash": self._hex(args[1]),
                            "expires_at": expires,
                        }
                    ),
                )
                return 1
            return 0
        if "SET healthy_at=CASE WHEN $1" in sql:
            worker = self._worker_by_id(str(args[2]))
            if worker is not None and worker.generation == self._int(args[3]) and worker.status == "active":
                good = bool(args[0])
                self._replace(
                    worker,
                    worker.model_copy(
                        update={
                            "healthy_at": self.now if good else None,
                            "catalog_fingerprint": str(args[1]) if good else None,
                        }
                    ),
                )
                return 1
            return 0
        if "UPDATE \"OicmMcpSource\" SET status='active'" in sql:
            for index, row in enumerate(self.sources):
                if row.source_id == str(args[0]) and row.status in {"pending", "quarantined"}:
                    self.sources[index] = row.model_copy(update={"status": "active"})
            return 1
        if 'INSERT INTO "OicmMcpRemoteWorker"' in sql:
            expires_inserted = args[8]
            assert isinstance(expires_inserted, datetime)
            assert "$9::timestamptz" in sql, (
                "expires_at must be cast for timestamptz; text binding is rejected by PostgreSQL"
            )
            assert "NOW()" in sql and "'active'" in sql, "membership must be born redeemed and active"
            self.workers.append(
                RemoteWorkerRow(
                    worker_id=str(args[0]),
                    source_id=str(args[1]),
                    label=str(args[2]),
                    target_host="127.0.0.1",
                    target_port=self._int(args[4]),
                    dataset_id=str(args[5]),
                    proof_key=str(args[6]),
                    token_hash=self._hex(args[7]),
                    expires_at=expires_inserted,
                    redeemed_at=self.now,
                    healthy_at=None,
                    catalog_fingerprint=None,
                    status="active",
                    generation=1,
                    enrollment_id=str(args[9]),
                )
            )
            return 1
        raise AssertionError(sql[:120])

    def _replace(self, old: RemoteWorkerRow, new: RemoteWorkerRow) -> None:
        self.workers[self.workers.index(old)] = new


@dataclass
class Broker:
    keys: Keys
    db: RemoteMemoryDatabase
    store: RemoteStore

    @property
    def worker(self) -> RemoteWorkerRow:
        assert len(self.db.workers) == 1
        return self.db.workers[0]


def make_broker() -> Broker:
    keys: Final = Keys(PEPPER)
    db: Final = RemoteMemoryDatabase()
    record: Final = SourceRecord(
        source_id="11111111-1111-1111-1111-111111111111",
        tenant="alice",
        key="k" * 64,
        alias="pets-api",
        target=Target(host="127.0.0.1", port=18080),
        spec_path="mcp-source-broker/spec.json",
        expected="f" * 64,
        gateway_auth_ciphertext="",
        status="pending",
        revision=1,
    )
    db.sources.append(record)

    class Store:
        def __init__(self) -> None:
            self.keyring = keys
            self.db = typing.cast(Database, db)  # noqa: TID251  # cast-ok: memory fake satisfies the SQL protocol

        async def source(self, source_id: str) -> SourceRecord | None:
            return next((s for s in db.sources if s.source_id == source_id), None)

    return Broker(keys=keys, db=db, store=RemoteStore(Store()))


def peer() -> RemotePeer:
    import base64

    public: Final = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    return RemotePeer(
        label="worker-a",
        dataset_id="postgres-primary",
        proof_key=base64.b64encode(public).decode(),
        target=Target(host="127.0.0.1", port=18080),
    )


@pytest.mark.asyncio
async def test_create_peer_activates_membership_without_installer_tokens() -> None:
    broker: Final = make_broker()
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert created is not None
    assert created.worker_id and created.label == "worker-a"
    assert "install_path" not in created.model_dump()
    assert "enrollment_token" not in created.model_dump()
    assert broker.worker.status == "active"
    assert broker.worker.redeemed_at is not None, "membership anchor is pre-redeemed; no installer redemption exists"


@pytest.mark.asyncio
async def test_report_admits_only_matching_catalog() -> None:
    broker: Final = make_broker()
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert created is not None
    admitted: Final = await broker.store.report(created.worker_id, 1, "f" * 64)
    assert admitted, "matching fingerprint must activate the source"
    assert broker.db.sources[0].status == "active"
    assert broker.worker.catalog_fingerprint == "f" * 64
    wrong: Final = await broker.store.report(created.worker_id, 1, "0" * 64)
    assert not wrong, "mismatched fingerprint must be rejected"
    stale: Final = await broker.store.report(created.worker_id, 2, "f" * 64)
    assert not stale, "stale generation must be rejected"


@pytest.mark.asyncio
async def test_lifecycle_drain_keeps_generation_and_recover_rotates_it() -> None:
    broker: Final = make_broker()
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert created is not None
    broker.db.workers[0] = broker.worker.model_copy(update={"status": "active"})
    drained: Final = await broker.store.lifecycle(created.worker_id, "drain", 1)
    assert drained is not None and drained.phase == "draining" and drained.generation == 1
    stale: Final = await broker.store.lifecycle(created.worker_id, "revoke", 99)
    assert stale is None, "stale generation must be rejected"
    recovered: Final = await broker.store.lifecycle(created.worker_id, "recover", 1)
    assert recovered is not None and recovered.phase == "active" and recovered.generation == 2
    assert "enrollment_token" not in recovered.model_dump()


@pytest.mark.asyncio
async def test_revoke_is_terminal() -> None:
    broker: Final = make_broker()
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert created is not None
    revoked: Final = await broker.store.lifecycle(created.worker_id, "revoke", 1)
    assert revoked is not None and revoked.phase == "revoked" and revoked.generation == 2
    assert broker.worker.healthy_at is None and broker.worker.catalog_fingerprint is None
    readmission: Final = await broker.store.lifecycle(created.worker_id, "recover", 2)
    assert readmission is None, "revoked workers must not be recoverable; a new membership is required"


@pytest.mark.asyncio
async def test_peers_publish_plain_sha256_digest_matching_go_contract() -> None:
    broker: Final = make_broker()
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert created is not None
    await broker.store.report(created.worker_id, 1, "f" * 64)
    listed: Final = await broker.store.peers()
    assert len(listed) == 1
    credential: Final = broker.keys.remote_credential(
        broker.worker.enrollment_id, broker.worker.worker_id, broker.worker.generation
    )
    assert listed[0].credential_digest == hashlib.sha256(credential.encode()).hexdigest()
    assert listed[0].phase == "active"
    assert listed[0].expected_catalog == "f" * 64


@pytest.mark.asyncio
async def test_create_peer_rejects_duplicate_label_and_revoked_source() -> None:
    broker: Final = make_broker()
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert created is not None
    duplicate: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert duplicate is None
    missing: Final = await broker.store.create_peer("missing-source", peer())
    assert missing is None


@pytest.mark.asyncio
async def test_control_app_peers_requires_bearer_header_not_query() -> None:
    """The Go worker sends the service token in the Authorization header."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from litellm.proxy.mcp_source_broker.remote_gateway import create_remote_control_app

    broker: Final = make_broker()
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert created is not None

    class StoreWithRemote:
        remote: RemoteStore = broker.store

    secret: Final = "service-secret-for-control-app"
    app: Final = FastAPI()
    app.mount("/", create_remote_control_app(StoreWithRemote(), secret))
    transport: Final = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied: Final = await client.get("/v1/remotedialer/peers")
        assert denied.status_code == 401, "missing header must be rejected"
        wrong_header: Final = await client.get(
            "/v1/remotedialer/peers", headers={"Authorization": f"Bearer wrong-{secret}"}
        )
        assert wrong_header.status_code == 401
        as_query: Final = await client.get("/v1/remotedialer/peers", params={"authorization": f"Bearer {secret}"})
        assert as_query.status_code == 401, "token passed as query parameter must not authenticate"
        ok: Final = await client.get("/v1/remotedialer/peers", headers={"Authorization": f"Bearer {secret}"})
        assert ok.status_code == 200
        listed: Final = ok.json()
        assert len(listed) == 1 and listed[0]["worker_id"] == created.worker_id
        reported: Final = await client.post(
            "/v1/remotedialer/report",
            headers={"Authorization": f"Bearer {secret}"},
            json={"worker_id": created.worker_id, "generation": 1, "catalog_fingerprint": "f" * 64},
        )
        assert reported.status_code == 200 and reported.json() == {"admitted": True}
        probe: Final = await client.get("/health")
        assert probe.status_code == 200, "kubelet probes must not need the service token"
