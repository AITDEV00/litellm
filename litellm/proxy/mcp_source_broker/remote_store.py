from __future__ import annotations

import hmac
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Final, Literal, Protocol
from uuid import uuid5

from pydantic import BaseModel, ConfigDict, TypeAdapter, field_validator

from litellm.proxy.mcp_source_broker.keys import Keys
from litellm.proxy.mcp_source_broker.models import (
    NAMESPACE,
    RemoteGoPeer,
    RemoteLifecycle,
    RemotePeer,
    RemotePeerResponse,
    SourceRecord,
)
from litellm.proxy.mcp_source_broker.store import Database, SQLDatabase, postgres_json_row


class RemoteWorkerRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    worker_id: str
    source_id: str
    label: str
    target_host: Literal["127.0.0.1"]
    target_port: int
    dataset_id: str
    proof_key: str
    token_hash: bytes
    expires_at: datetime
    redeemed_at: datetime | None
    healthy_at: datetime | None
    catalog_fingerprint: str | None
    status: Literal["pending", "active", "draining", "revoked"]
    generation: int
    enrollment_id: str
    delivered_at: datetime | None = None


REMOTE_WORKERS: Final = TypeAdapter(tuple[RemoteWorkerRow, ...])


class RemotePeerProjection(BaseModel):
    model_config = ConfigDict(frozen=True)
    worker: RemoteWorkerRow
    expected: str

    @field_validator("worker", mode="before")
    @classmethod
    def decode_worker(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        return postgres_json_row(value)


class RemoteSources(Protocol):
    db: Database
    keyring: Keys

    async def source(self, source_id: str) -> SourceRecord | None: ...


REMOTE_COLUMNS: Final = (
    "worker_id,source_id,label,target_host,target_port,dataset_id,proof_key,token_hash,expires_at,"
    "redeemed_at,healthy_at,catalog_fingerprint,status,generation,enrollment_id,delivered_at"
)

INSERT_COLUMNS: Final = (
    "worker_id,source_id,label,target_host,target_port,dataset_id,proof_key,token_hash,expires_at,"
    "redeemed_at,healthy_at,catalog_fingerprint,status,generation,enrollment_id"
)


class RemoteStore:
    def __init__(self, sources: RemoteSources) -> None:
        self.sources: Final = sources
        self.db: Final = sources.db

    @property
    def keys(self) -> Keys:
        return self.sources.keyring

    def _rows(self, value: object) -> Sequence[Mapping[str, object]]:
        return TypeAdapter(tuple[dict[str, object], ...]).validate_python(value)

    async def worker(self, worker_id: str, db: SQLDatabase | None = None) -> RemoteWorkerRow | None:
        records: Final = REMOTE_WORKERS.validate_python(
            tuple(
                postgres_json_row(row)
                for row in self._rows(
                    await (db or self.db).query_raw(
                        "SELECT " + REMOTE_COLUMNS + ' FROM "OicmMcpRemoteWorker" WHERE worker_id=$1', worker_id
                    )
                )
            )
        )
        return records[0] if records else None

    async def create_peer(self, source_id: str, peer: RemotePeer) -> RemotePeerResponse | None:
        source: Final = await self.sources.source(source_id)
        if source is None or source.status == "revoked":
            return None
        async with self.db.tx(timeout=10000) as tx:
            await tx.execute_raw("SELECT pg_advisory_xact_lock(hashtext($1))", source_id)
            found: Final = self._rows(
                await tx.query_raw(
                    'SELECT worker_id FROM "OicmMcpRemoteWorker" WHERE source_id=$1 AND label=$2', source_id, peer.label
                )
            )
            if found:
                return None
            worker_id: Final = str(uuid5(NAMESPACE, f"remote-worker:{source_id}:{peer.label}"))
            grant_id: Final = self.keys.enrollment_id(source_id)
            generation: Final = 1
            expires: Final = datetime.now(timezone.utc) + timedelta(minutes=10)
            await tx.execute_raw(
                'INSERT INTO "OicmMcpRemoteWorker" (' + INSERT_COLUMNS + ") "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8::bytea,$9::timestamptz,NOW(),NULL,NULL,'active',$11,$10)",
                worker_id,
                source_id,
                peer.label,
                peer.target.host,
                peer.target.port,
                peer.dataset_id,
                peer.proof_key,
                "\\x" + self.keys.token_hash(grant_id, self.keys.mint_token()).hex(),
                expires,
                grant_id,
                generation,
            )
            return RemotePeerResponse(
                worker_id=worker_id,
                source_id=source_id,
                target=peer.target.host + ":" + str(peer.target.port),
                generation=generation,
                credential=self.keys.remote_credential(grant_id, worker_id, generation),
                expires_at=expires,
                label=peer.label,
                dataset_id=peer.dataset_id,
            )

    async def resume_peer(self, source_id: str, peer: RemotePeer) -> RemotePeerResponse | None:
        """Re-derive the credential for an already-issued worker at its live generation.

        A first bootstrap can enroll the source, create the worker row, and then
        lose the response or fail to persist the identity. The label is
        deterministic, so the same operator asking again is asking for the same
        worker, not a new one. This reissues that worker's credential without
        inserting a row, advancing the generation, or changing status, which keeps
        the "duplicate label is a 409" rule intact for a *different* caller while
        giving the lost-credential path a defined recovery.

        Resume is bound to the *whole* registration fingerprint: label, target,
        dataset id and proof key must all match the stored row. Matching only the
        label and target would let a caller who changed the dataset or origin
        proof silently receive the old credential, which is a different
        registration than the one on record. A mismatch is therefore "nothing to
        resume", not "reissue the old one".

        Resume is also bounded to a *proven-lost* bootstrap response. Once
        ``delivered_at`` is set, a report with the matching catalog fingerprint has
        shown the worker actually received and used this credential, so a later
        request for it is a replay, not recovery. Re-issuing then would hand out the
        same live HMAC indefinitely to whoever asks. A delivered worker therefore
        resolves through revocation plus a fresh registration instead.

        Returns None when there is nothing safe to resume: no worker for the
        label, any fingerprint field differing, a worker that is revoked or
        draining, or one whose credential was already delivered. Those are the
        operator-action-needed terminal states, not retryable ones.
        """
        source: Final = await self.sources.source(source_id)
        if source is None or source.status == "revoked":
            return None
        async with self.db.tx(timeout=10000) as tx:
            await tx.execute_raw("SELECT pg_advisory_xact_lock(hashtext($1))", source_id)
            rows: Final = REMOTE_WORKERS.validate_python(
                tuple(
                    postgres_json_row(row)
                    for row in self._rows(
                        await tx.query_raw(
                            "SELECT " + REMOTE_COLUMNS + ' FROM "OicmMcpRemoteWorker" WHERE source_id=$1 AND label=$2',
                            source_id,
                            peer.label,
                        )
                    )
                )
            )
            found: Final = rows[0] if rows else None
            if found is None:
                return None
            if (
                found.target_host != peer.target.host
                or found.target_port != peer.target.port
                or found.dataset_id != peer.dataset_id
                or found.proof_key != peer.proof_key
            ):
                return None
            if found.status not in {"pending", "active"}:
                return None
            if found.delivered_at is not None:
                return None
            return RemotePeerResponse(
                worker_id=found.worker_id,
                source_id=source_id,
                target=f"{found.target_host}:{found.target_port}",
                generation=found.generation,
                credential=self.keys.remote_credential(found.enrollment_id, found.worker_id, found.generation),
                expires_at=found.expires_at,
                label=found.label,
                dataset_id=found.dataset_id,
            )

    async def lifecycle(
        self,
        worker_id: str,
        action: Literal["drain", "revoke", "recover"],
        generation: int,
    ) -> RemoteLifecycle | None:
        PHASES: Final[Mapping[Literal["drain", "revoke", "recover"], Literal["draining", "revoked", "active"]]] = {
            "drain": "draining",
            "revoke": "revoked",
            "recover": "active",
        }
        phase: Final = PHASES[action]
        if generation < 1:
            return None

        result_phase: Final = phase
        async with self.db.tx(timeout=10000) as tx:
            worker: Final = await self.worker(worker_id, tx)
            if (
                worker is None
                or worker.generation != generation
                or worker.status not in {"active", "draining", "pending"}
            ):
                return None
            next_generation: Final = worker.generation + int(action in {"revoke", "recover"})
            await tx.execute_raw(
                'UPDATE "OicmMcpRemoteWorker" SET generation=$2,status=$3 WHERE worker_id=$1',
                worker_id,
                next_generation,
                phase,
            )
            if action in {"revoke", "recover"}:
                await tx.execute_raw(
                    'UPDATE "OicmMcpRemoteWorker" SET healthy_at=NULL,catalog_fingerprint=NULL,token_hash=$2::bytea,'
                    "expires_at=$3::timestamptz WHERE worker_id=$1",
                    worker_id,
                    "\\x" + self.keys.token_hash(worker.enrollment_id, self.keys.mint_token()).hex(),
                    datetime.now(timezone.utc) + timedelta(minutes=10),
                )
            return RemoteLifecycle(
                worker_id=worker_id, source_id=worker.source_id, phase=result_phase, generation=next_generation
            )

    async def report(self, worker_id: str, generation: int, fingerprint: str | None) -> bool:
        worker: Final = await self.worker(worker_id)
        source: Final = None if worker is None else await self.sources.source(worker.source_id)
        if worker is None or source is None or worker.status != "active" or worker.generation != generation:
            return False
        good: Final = fingerprint is not None and hmac.compare_digest(fingerprint, source.expected)
        async with self.db.tx(timeout=10000) as tx:
            await tx.execute_raw(
                'UPDATE "OicmMcpRemoteWorker" SET healthy_at=CASE WHEN $1 THEN NOW() ELSE NULL END,'
                "catalog_fingerprint=CASE WHEN $1 THEN $2 ELSE NULL END "
                "WHERE worker_id=$3 AND generation=$4 AND status='active'",
                good,
                fingerprint,
                worker_id,
                generation,
            )
            if good:
                await tx.execute_raw(
                    "UPDATE \"OicmMcpRemoteWorker\" SET delivered_at=COALESCE(delivered_at, NOW()) "
                    "WHERE worker_id=$1 AND generation=$2",
                    worker_id,
                    generation,
                )
                await tx.execute_raw(
                    "UPDATE \"OicmMcpSource\" SET status='active' WHERE source_id=$1 AND status IN ('pending','quarantined')",
                    worker.source_id,
                )
        return good

    async def peers(self) -> Sequence[RemoteGoPeer]:
        raw: Final = self._rows(
            await self.db.query_raw(
                'SELECT row_to_json(w) AS worker,s.expected FROM "OicmMcpRemoteWorker" w '
                'JOIN "OicmMcpSource" s ON s.source_id=w.source_id ORDER BY w.worker_id'
            )
        )
        projections: Final = tuple(
            RemotePeerProjection.model_validate(
                {
                    "worker": row["worker"],
                    "expected": row["expected"],
                }
            )
            for row in raw
        )
        return tuple(
            RemoteGoPeer(
                worker_id=projection.worker.worker_id,
                source_id=projection.worker.source_id,
                label=projection.worker.label,
                target=f"{projection.worker.target_host}:{projection.worker.target_port}",
                dataset_id=projection.worker.dataset_id,
                proof_key=projection.worker.proof_key,
                generation=projection.worker.generation,
                phase=projection.worker.status,
                credential_digest=self.keys.remote_digest(
                    projection.worker.enrollment_id, projection.worker.worker_id, projection.worker.generation
                ),
                expected_catalog=projection.expected,
                catalog_fingerprint=projection.worker.catalog_fingerprint,
                healthy_at=projection.worker.healthy_at,
            )
            for projection in projections
            if projection.worker.redeemed_at is not None
        )
