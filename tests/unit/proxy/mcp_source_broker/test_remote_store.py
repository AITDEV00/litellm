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
from litellm.proxy.mcp_source_broker.remote_store import REMOTE_COLUMNS, RemoteStore, RemoteWorkerRow
from litellm.proxy.mcp_source_broker.store import Database, postgres_json_row

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

    @staticmethod
    def _insert_columns(sql: str) -> int:
        columns: Final = sql.split("(", 1)[1].split(")", 1)[0]
        return len([column for column in columns.split(",") if column.strip()])

    @staticmethod
    def _insert_value_count(sql: str) -> int:
        values: Final = sql.rsplit("VALUES", 1)[1]
        body: Final = values[values.index("(") + 1 : values.rindex(")")]
        depth: Final = 0
        count: Final = 1
        for char in body:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                count += 1
        return count if body.strip() else 0

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
                rows.append({"worker": worker.model_dump(), "expected": source.expected, "kind": source.kind})
            return rows
        if "SELECT worker_id FROM" in sql:
            return [{"worker_id": w.worker_id} for w in self.workers if w.source_id == args[0] and w.label == args[1]]
        if "WHERE source_id=$1 AND label=$2" in sql:
            return [w.model_dump() for w in self.workers if w.source_id == args[0] and w.label == args[1]]
        raise AssertionError(sql[:120])

    async def execute_raw(self, sql: str, *args: object) -> int:
        if "CREATE TABLE" in sql or "ADD COLUMN" in sql or "pg_advisory_xact_lock" in sql:
            return 0
        if "SET healthy_at=NULL,catalog_fingerprint=NULL" in sql and "$2::bytea" not in sql:
            for worker in self.workers:
                self._replace(
                    worker,
                    worker.model_copy(update={"healthy_at": None, "catalog_fingerprint": None}),
                )
            return len(self.workers)
        if "SET delivered_at=COALESCE(delivered_at, NOW())" in sql:
            worker = self._worker_by_id(str(args[0]))
            if worker is not None and worker.generation == self._int(args[1]):
                self._replace(worker, worker.model_copy(update={"delivered_at": worker.delivered_at or self.now}))
                return 1
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
                            "catalog_fingerprint": (None if args[1] is None else str(args[1])) if good else None,
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
            columns = self._insert_columns(sql)
            values = self._insert_value_count(sql)
            assert columns == values, (
                "the worker INSERT lists %d columns but binds %d values; PostgreSQL rejects that with "
                "'INSERT has more target columns than expressions'" % (columns, values)
            )
            self.workers.append(
                RemoteWorkerRow(
                    worker_id=str(args[0]),
                    source_id=str(args[1]),
                    label=str(args[2]),
                    target_host=str(args[3]),
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
async def test_mcp_report_without_a_catalog_marks_the_source_healthy() -> None:
    """An MCP source has no OpenAPI catalog, so its health report carries no
    fingerprint. A successful report on the right generation must still make it
    healthy, otherwise an MCP source can never leave the unhealthy state."""
    broker: Final = make_broker()
    broker.db.sources[0] = broker.db.sources[0].model_copy(update={"kind": "mcp"})  # mutable-ok: test fake row list
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert created is not None
    admitted: Final = await broker.store.report(created.worker_id, 1, None)
    assert admitted, "a reachable MCP source must be admitted without a catalog"
    assert broker.db.sources[0].status == "active"
    assert broker.worker.healthy_at is not None
    assert broker.worker.catalog_fingerprint is None
    stale: Final = await broker.store.report(created.worker_id, 2, None)
    assert not stale, "stale generation must still be rejected for an MCP source"


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
async def test_peers_carry_source_kind_so_the_sidecar_skips_the_catalog_probe() -> None:
    """An MCP source has no OpenAPI catalog. The peer must publish kind so the
    sidecar probes its MCP endpoint instead of /openapi.json, which 404s."""
    broker: Final = make_broker()
    mcp_source: Final = broker.db.sources[0].model_copy(update={"kind": "mcp"})
    broker.db.sources[0] = mcp_source  # mutable-ok: test fake row list
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert created is not None
    listed: Final = await broker.store.peers()
    assert len(listed) == 1
    assert listed[0].kind == "mcp"


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
async def test_resume_peer_rederives_the_same_credential_without_new_state() -> None:
    """A lost worker response must be recoverable from what was already stored.

    Resume is only worth having if it hands back the very credential the first
    call issued, at the same generation, without adding a row: anything else would
    either break the fencing the duplicate-label rule protects or quietly mint a
    second identity for one label.
    """
    broker: Final = make_broker()
    registered: Final = peer()
    issued: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", registered)
    assert issued is not None
    rows_before: Final = len(broker.db.workers)

    resumed: Final = await broker.store.resume_peer("11111111-1111-1111-1111-111111111111", registered)

    assert resumed is not None
    assert resumed.worker_id == issued.worker_id
    assert resumed.generation == issued.generation
    assert resumed.credential == issued.credential, "the re-derived credential must be identical to the issued one"
    assert len(broker.db.workers) == rows_before, "resume must not insert a worker row"
    assert broker.worker.generation == 1, "resume must not advance the generation"


@pytest.mark.asyncio
async def test_legacy_preserved_label_resumes_its_original_worker() -> None:
    """A connector reregistering a historical dotted alias must find its worker.

    The Go connector's WorkerLabel preserves the pre-digest reduction for names
    that fit it (``alpha.127.0.0.1.18080`` -> ``alpha-127-0-0-1-18080``), so the
    lost-credential path recomputes the label the server already keyed. This test
    carries that exact label through the real store path: issue with it, then
    resume with it while the credential is undelivered, and assert the server
    selects the same identity rather than a new association.
    """
    broker: Final = make_broker()
    registered: Final = peer()
    registered = registered.model_copy(update={"label": "alpha-127-0-0-1-18080"})
    # The digest-bearing sibling is inserted FIRST, so any query that loosens the
    # label match would deterministically select it: the exact-label binding is
    # what keeps recovery on the historical worker.
    digest_peer: Final = registered.model_copy(update={"label": "alpha-127-0-0-1-18080-0123456789"})
    sibling: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", digest_peer)
    assert sibling is not None
    issued: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", registered)
    assert issued is not None
    assert issued.label == "alpha-127-0-0-1-18080"
    assert issued.worker_id != sibling.worker_id

    resumed: Final = await broker.store.resume_peer("11111111-1111-1111-1111-111111111111", registered)

    assert resumed is not None, "the preserved historical label must resume its original worker"
    assert resumed.worker_id == issued.worker_id
    assert resumed.source_id == issued.source_id
    assert resumed.generation == issued.generation
    assert resumed.credential == issued.credential
    assert len(broker.db.workers) == 2, "resume selects the stored worker, never a new association"

    # Once delivery is proven, the same registration is a replay and must be
    # refused: the historical label is not a credential-recovery loophole.
    assert await broker.store.report(issued.worker_id, issued.generation, "f" * 64) is True
    delivered_row: Final = next(w for w in broker.db.workers if w.worker_id == issued.worker_id)
    assert delivered_row.delivered_at is not None
    replayed: Final = await broker.store.resume_peer("11111111-1111-1111-1111-111111111111", registered)
    assert replayed is None, "a delivered worker credential must not be reissued on replay"


@pytest.mark.asyncio
async def test_resume_peer_refuses_a_target_that_no_longer_matches() -> None:
    """A changed loopback target is a different origin, so it must not inherit the
    stored worker's credential."""
    broker: Final = make_broker()
    registered: Final = peer()
    assert await broker.store.create_peer("11111111-1111-1111-1111-111111111111", registered) is not None
    moved_peer: Final = registered.model_copy(update={"target": Target(host="127.0.0.1", port=18081)})
    moved: Final = await broker.store.resume_peer("11111111-1111-1111-1111-111111111111", moved_peer)
    assert moved is None


@pytest.mark.asyncio
async def test_resume_peer_refuses_a_changed_dataset_so_it_cannot_steal_the_credential() -> None:
    """Resume is bound to the full registration fingerprint.

    Before the fix it matched only source/label/target, so a caller who changed the
    dataset silently received the old worker's credential for a registration the
    gateway never approved. A changed dataset is a different registration, so it
    must not resume.
    """
    broker: Final = make_broker()
    registered: Final = peer()
    assert await broker.store.create_peer("11111111-1111-1111-1111-111111111111", registered) is not None
    changed: Final = registered.model_copy(update={"dataset_id": "postgres-replica"})
    resumed: Final = await broker.store.resume_peer("11111111-1111-1111-1111-111111111111", changed)
    assert resumed is None, "a changed dataset must not inherit the stored credential"


@pytest.mark.asyncio
async def test_resume_peer_refuses_a_changed_proof_key_so_trust_drift_is_not_hidden() -> None:
    """A different origin proof public key is a different trust anchor. Resuming it
    would hand the old credential to a caller whose proof key the gateway never
    validated for this worker."""
    broker: Final = make_broker()
    registered: Final = peer()
    assert await broker.store.create_peer("11111111-1111-1111-1111-111111111111", registered) is not None
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    other_public: Final = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    changed: Final = registered.model_copy(update={"proof_key": base64.b64encode(other_public).decode()})
    resumed: Final = await broker.store.resume_peer("11111111-1111-1111-1111-111111111111", changed)
    assert resumed is None, "a changed proof key must not inherit the stored credential"


@pytest.mark.asyncio
async def test_resume_peer_rederives_only_for_an_exactly_identical_registration() -> None:
    """The positive control: an identical registration still resumes in place, so the
    fingerprint check did not just break recovery."""
    broker: Final = make_broker()
    registered: Final = peer()
    issued: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", registered)
    assert issued is not None
    resumed: Final = await broker.store.resume_peer("11111111-1111-1111-1111-111111111111", registered)
    assert resumed is not None and resumed.worker_id == issued.worker_id


@pytest.mark.asyncio
async def test_resume_peer_refuses_once_the_credential_was_proven_delivered() -> None:
    """A delivered credential must not be exportable again on request.

    ``delivered_at`` is the evidence that the worker received and used the
    credential (a report with the matching catalog fingerprint). Before this bound,
    any owner could replay the identical registration and re-derive the same live
    HMAC forever, which is a standing export of a secret, not recovery from a lost
    response. Recovery after delivery is revocation plus a fresh registration.
    """
    broker: Final = make_broker()
    registered: Final = peer()
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", registered)
    assert created is not None
    assert broker.worker.delivered_at is None

    assert await broker.store.resume_peer("11111111-1111-1111-1111-111111111111", registered) is not None

    assert await broker.store.report(created.worker_id, 1, "f" * 64)
    reissued: Final = await broker.store.resume_peer("11111111-1111-1111-1111-111111111111", registered)

    assert reissued is None, "a delivered credential must not be handed out again on replay"


@pytest.mark.asyncio
async def test_resume_peer_refuses_a_revoked_worker_so_replacement_needs_a_new_label() -> None:
    """Revocation is terminal by decision, so resume must decline rather than
    resurrect the revoked credential. The supported replacement is a fresh label,
    and the revoked generation stays fenced."""
    broker: Final = make_broker()
    registered: Final = peer()
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", registered)
    assert created is not None
    revoked: Final = await broker.store.lifecycle(created.worker_id, "revoke", 1)
    assert revoked is not None and revoked.phase == "revoked"

    resumed: Final = await broker.store.resume_peer("11111111-1111-1111-1111-111111111111", registered)
    assert resumed is None, "a revoked worker must never resume"

    replacement_peer: Final = peer().model_copy(update={"label": "worker-b"})
    replacement: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", replacement_peer)
    assert replacement is not None and replacement.generation == 1, "a fresh label starts a new membership"
    assert replacement.worker_id != created.worker_id

    keep_out: Final = await broker.store.report(created.worker_id, revoked.generation, "f" * 64)
    assert not keep_out, "the revoked generation must not be admissible even after a replacement exists"


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
        denied: Final = await client.get("/v1/source-link/peers")
        assert denied.status_code == 401, "missing header must be rejected"
        wrong_header: Final = await client.get(
            "/v1/source-link/peers", headers={"Authorization": f"Bearer wrong-{secret}"}
        )
        assert wrong_header.status_code == 401
        as_query: Final = await client.get("/v1/source-link/peers", params={"authorization": f"Bearer {secret}"})
        assert as_query.status_code == 401, "token passed as query parameter must not authenticate"
        ok: Final = await client.get("/v1/source-link/peers", headers={"Authorization": f"Bearer {secret}"})
        assert ok.status_code == 200
        listed: Final = ok.json()
        assert len(listed) == 1 and listed[0]["worker_id"] == created.worker_id
        reported: Final = await client.post(
            "/v1/source-link/report",
            headers={"Authorization": f"Bearer {secret}"},
            json={"worker_id": created.worker_id, "generation": 1, "catalog_fingerprint": "f" * 64},
        )
        assert reported.status_code == 200 and reported.json() == {"admitted": True}
        probe: Final = await client.get("/health")
        assert probe.status_code == 200, "kubelet probes must not need the service token"


@pytest.mark.asyncio
async def test_first_delivery_is_acknowledged_when_the_worker_proves_its_credential() -> None:
    """R8: delivery is recorded from evidence, not from a guess.

    A worker that reports a matching catalog fingerprint has demonstrably received
    and used its credential, which is the only trustworthy signal that delivery
    happened. Recording it at issue time or from a process User-Agent would be
    guessing.
    """
    broker: Final = make_broker()
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert created is not None
    assert broker.worker.delivered_at is None, "nothing has proven delivery yet"

    admitted: Final = await broker.store.report(created.worker_id, 1, "f" * 64)

    assert admitted
    assert broker.worker.delivered_at is not None, "a successful credential use is the acknowledgement"


@pytest.mark.asyncio
async def test_delivery_acknowledgement_is_recorded_once_and_is_not_reset_by_a_later_report() -> None:
    """The timestamp is set with COALESCE, so a second good report keeps the first
    delivery time rather than moving it, which is what makes it a delivery record
    instead of a last-seen clock."""
    broker: Final = make_broker()
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", peer())
    assert created is not None

    await broker.store.report(created.worker_id, 1, "f" * 64)
    first: Final = broker.worker.delivered_at
    await broker.store.report(created.worker_id, 1, "f" * 64)

    assert broker.worker.delivered_at == first


def test_postgres_json_row_converts_every_timestamp_column_the_peers_query_selects() -> None:
    """`row_to_json` hands back ISO strings, while Prisma's native query returns
    datetimes. The conversion helper must cover every timestamp column
    `REMOTE_COLUMNS` selects, or a newly added one (like `delivered_at`) reaches the
    projection validator as a string and rejects the row. Derive the check from that
    column tuple so adding a column without updating the conversion fails here.
    """
    timestamp_columns: Final = tuple(
        column for column in REMOTE_COLUMNS.split(",") if column == "delivered_at" or column.endswith("_at")
    )
    assert "delivered_at" in timestamp_columns

    row: Final = postgres_json_row(
        {column: "2026-11-19T00:00:00+00:00" for column in timestamp_columns} | {"worker_id": "w"}
    )

    for column in timestamp_columns:
        assert isinstance(row[column], datetime), f"{column} must convert to a datetime, not stay a string"


def _broker_with_source(target: Target) -> Broker:
    """A broker whose single source is reviewed at ``target``."""
    keys: Final = Keys(PEPPER)
    db: Final = RemoteMemoryDatabase()
    db.sources.append(
        SourceRecord(
            source_id="11111111-1111-1111-1111-111111111111",
            tenant="alice",
            key="k" * 64,
            alias="pets-api",
            target=target,
            spec_path="mcp-source-broker/spec.json",
            expected="f" * 64,
            gateway_auth_ciphertext="",
            status="pending",
            revision=1,
        )
    )

    class Store:
        def __init__(self) -> None:
            self.keyring = keys
            self.db = typing.cast(Database, db)

        async def source(self, source_id: str) -> SourceRecord | None:
            return next((s for s in db.sources if s.source_id == source_id), None)

    return Broker(keys=keys, db=db, store=RemoteStore(Store()))


def _plain_peer(target: Target) -> RemotePeer:
    return RemotePeer(label="worker-a", dataset_id="ds", target=target)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,expected_canonical",
    [
        (Target(host="10.34.104.99", port=9001), "10.34.104.99:9001"),
        (Target(host="inventory.default.svc", port=9001), "inventory.default.svc:9001"),
        (Target(host="::1", port=9001), "[::1]:9001"),
        (Target(host="2001:db8::1", port=9001), "[2001:db8::1]:9001"),
    ],
)
async def test_worker_target_survives_insert_read_and_projection(target: Target, expected_canonical: str) -> None:
    """A service DNS, a private IPv4, and an IPv6 worker must round-trip.

    Previously the row DTO pinned the host to the literal ``127.0.0.1`` and
    ``peers()`` rendered ``host:port`` without brackets, so any non-loopback
    source either could not be stored or produced an address the connector could
    not parse. This pins insert -> read -> projection for each accepted shape.
    """
    broker: Final = _broker_with_source(target)
    created: Final = await broker.store.create_peer("11111111-1111-1111-1111-111111111111", _plain_peer(target))
    assert created is not None
    assert created.target == expected_canonical, "the response target must be canonical, IPv6 bracketed"

    read_back: Final = await broker.store.worker(created.worker_id)
    assert read_back is not None
    assert read_back.target_host == target.host

    peers: Final = await broker.store.peers()
    assert [p.target for p in peers] == [expected_canonical]


@pytest.mark.asyncio
async def test_create_peer_rejects_a_target_other_than_the_reviewed_source() -> None:
    """The worker must serve exactly the reviewed source target.

    A source approved at 127.0.0.1:9001 must refuse a worker for 127.0.0.1:9002
    before any side effect, so approval still describes what is exposed.
    """
    from litellm.proxy.mcp_source_broker.remote_store import PeerTargetMismatch

    broker: Final = _broker_with_source(Target(host="127.0.0.1", port=9001))
    with pytest.raises(PeerTargetMismatch) as raised:
        await broker.store.create_peer(
            "11111111-1111-1111-1111-111111111111", _plain_peer(Target(host="127.0.0.1", port=9002))
        )
    assert "127.0.0.1:9001" in str(raised.value)
    assert broker.db.workers == [], "a mismatched worker must not be inserted"


@pytest.mark.asyncio
async def test_peers_isolate_a_malformed_row_instead_of_failing_all() -> None:
    """One corrupt worker row must not take every other peer down.

    A bad status literal or unparseable field in a single row previously made the
    whole ``peers()`` projection raise, stranding every connector. The bad row is
    dropped and the healthy row still projects.
    """
    broker: Final = _broker_with_source(Target(host="127.0.0.1", port=9001))
    good: Final = await broker.store.create_peer(
        "11111111-1111-1111-1111-111111111111", _plain_peer(Target(host="127.0.0.1", port=9001))
    )
    assert good is not None
    bad: Final = RemoteWorkerRow.model_construct(
        worker_id="bad",
        source_id="11111111-1111-1111-1111-111111111111",
        label="bad",
        target_host="127.0.0.1",
        target_port=9001,
        dataset_id="ds",
        proof_key="",
        token_hash=b"\x00",
        expires_at=broker.db.now,
        redeemed_at=broker.db.now,
        healthy_at=None,
        catalog_fingerprint=None,
        status="not-a-status",
        generation=1,
        enrollment_id="e",
        delivered_at=None,
    )
    broker.db.workers.append(bad)
    peers: Final = await broker.store.peers()
    assert [p.worker_id for p in peers] == [good.worker_id], "the healthy peer must survive a malformed sibling"


@pytest.mark.asyncio
async def test_reset_health_clears_content_proof_and_liveness_at_startup() -> None:
    """Broker restart must clear the persisted fingerprint, not only healthy_at.

    A stored catalog fingerprint describes the last catalog a worker proved. If it
    survived restart it could stand in for a fresh probe, so a source would look
    content-proven before any connector reconnected. Both columns are cleared so
    nothing is healthy until a new connector proves its origin again.
    """
    broker: Final = make_broker()
    created: Final = await broker.store.create_peer(
        "11111111-1111-1111-1111-111111111111", _plain_peer(Target(host="127.0.0.1", port=18080))
    )
    assert created is not None
    worker: Final = broker.db.workers[0]
    broker.db._replace(worker, worker.model_copy(update={"healthy_at": broker.db.now, "catalog_fingerprint": "f" * 64}))
    assert broker.db.workers[0].catalog_fingerprint == "f" * 64

    await broker.store.reset_health()

    after: Final = broker.db.workers[0]
    assert after.healthy_at is None, "healthy_at must be cleared at startup"
    assert after.catalog_fingerprint is None, "catalog_fingerprint must be cleared at startup, not just healthy_at"

    # A retained source row plus a retained worker row must still project as
    # not-healthy after restart; the next successful report re-establishes it.
    peers: Final = await broker.store.peers()
    assert len(peers) == 1
    assert peers[0].healthy_at is None and peers[0].catalog_fingerprint is None, (
        "a restart must require a fresh probe, not inherit a previous boot's health"
    )
    await broker.store.report(worker.worker_id, 1, "f" * 64)
    reconnected: Final = await broker.store.peers()
    assert reconnected[0].catalog_fingerprint == "f" * 64, "a fresh report must re-establish content proof"
