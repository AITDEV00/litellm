from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, TracebackType
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, TypeAdapter
from typing_extensions import LiteralString

from litellm.proxy.mcp_source_broker.keys import Keys
from litellm.proxy.mcp_source_broker.models import (
    RouteSelection,
    SourceCreate,
    SourceRecord,
    Target,
    TransportName,
)


class SQLDatabase(Protocol):
    async def query_raw(self, query: LiteralString, *args: object) -> object: ...
    async def execute_raw(self, query: LiteralString, *args: object) -> int: ...


class Transaction(Protocol):
    async def __aenter__(self) -> SQLDatabase: ...

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class Database(SQLDatabase, Protocol):
    def tx(self, *, timeout: int | timedelta = ...) -> Transaction: ...


class SQLRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_id: str
    tenant: str
    key: str
    alias: str
    target_host: Literal["127.0.0.1"]
    target_port: int
    spec_path: str
    expected: str
    gateway_auth_ciphertext: str
    status: str
    revision: int

    def value(self) -> SourceRecord:
        return SourceRecord(
            source_id=self.source_id,
            tenant=self.tenant,
            key=self.key,
            alias=self.alias,
            target=Target(host=self.target_host, port=self.target_port),
            spec_path=self.spec_path,
            expected=self.expected,
            gateway_auth_ciphertext=self.gateway_auth_ciphertext,
            status=self.status,
            revision=self.revision,
        )


SOURCES: Final = TypeAdapter(tuple[SQLRow, ...])
SOURCE_COLUMNS: Final = (
    "source_id,tenant,key,alias,target_host,target_port,spec_path,expected,gateway_auth_ciphertext,status,revision"
)


@dataclass(frozen=True, slots=True)
class NotOwnerConflict:
    id: str


@dataclass(frozen=True)
class ContentConflict:
    """An existing source was re-enrolled with different content.

    The stored row keeps its original target and catalog fingerprint. Re-review of
    changed content is a deliberate, separate act; a retry must not achieve it by
    silently overwriting the artifact an already-approved row points at.
    """

    id: str


class PermissionDenied(Exception):
    pass


def postgres_bytes(value: bytes | str) -> bytes:
    if isinstance(value, bytes):
        return value
    return bytes.fromhex(value.removeprefix("\\x")) if value.startswith("\\x") else base64.b64decode(value)


def postgres_json_row(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(
        {
            field: postgres_bytes(item)
            if isinstance(item, (str, bytes)) and field in {"token_hash", "credential_digest"}
            else item
            for field, item in value.items()
        }
        | {
            field: datetime.fromisoformat(item)
            for field, item in value.items()
            if isinstance(item, str) and field in {"expires_at", "redeemed_at", "healthy_at"}
        }
    )


class SourceStore:
    def __init__(self, db: Database, keyring: Keys) -> None:
        self.db: Final = db
        self.keyring: Final = keyring
        from litellm.proxy.mcp_source_broker.remote_store import RemoteStore

        self.remote: Final = RemoteStore(self)

    async def route(self, source_id: str, transport: TransportName) -> None:
        await self.db.execute_raw(
            'UPDATE "OicmMcpSource" SET remote_transport=$2,revision=revision+1 '
            "WHERE source_id=$1 AND remote_transport IS DISTINCT FROM $2",
            source_id,
            transport,
        )

    async def selection(self, source_id: str) -> RouteSelection:
        rows: Final = TypeAdapter(tuple[dict[str, object], ...]).validate_python(
            await self.db.query_raw('SELECT remote_transport FROM "OicmMcpSource" WHERE source_id=$1', source_id)
        )
        if not rows:
            raise KeyError(source_id)
        transport: Final = rows[0]["remote_transport"]
        # The tunnel is the only data plane, so any stored value other than
        # "tunnel" (the retired "remotedialer" and "reverse-h2" literals
        # included) selects it. Reading them keeps a pre-transition row usable
        # without a data migration.
        if transport not in ("tunnel", "remotedialer", "reverse-h2"):
            raise ValueError(f"unsupported stored transport {transport!r}")
        return RouteSelection(transport="tunnel")

    async def _sources(self, db: SQLDatabase, query: LiteralString, *args: object) -> Sequence[SourceRecord]:
        return tuple(row.value() for row in SOURCES.validate_python(await db.query_raw(query, *args)))

    async def ensure(self) -> None:
        ordered: Final = (
            """CREATE TABLE IF NOT EXISTS "OicmMcpSource" (source_id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
               key TEXT NOT NULL, alias TEXT NOT NULL, target_host TEXT NOT NULL, target_port INT NOT NULL,
               spec_path TEXT NOT NULL, expected TEXT NOT NULL, gateway_auth_ciphertext TEXT NOT NULL,
               status TEXT NOT NULL, revision BIGINT NOT NULL, remote_transport TEXT NOT NULL DEFAULT 'tunnel',
               CONSTRAINT source_owner_alias UNIQUE (tenant,alias))""",
            """CREATE TABLE IF NOT EXISTS "OicmMcpEnrollment" (id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE
               REFERENCES "OicmMcpSource"(source_id), token_hash BYTEA NOT NULL,
               expires_at TIMESTAMPTZ NOT NULL, redeemed_at TIMESTAMPTZ)""",
            """CREATE TABLE IF NOT EXISTS "OicmMcpRemoteWorker" (worker_id TEXT PRIMARY KEY, enrollment_id TEXT NOT NULL
               REFERENCES "OicmMcpEnrollment"(id), source_id TEXT NOT NULL REFERENCES "OicmMcpSource"(source_id),
               label TEXT NOT NULL, target_host TEXT NOT NULL, target_port INT NOT NULL, dataset_id TEXT NOT NULL,
               proof_key TEXT NOT NULL, token_hash BYTEA NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
               redeemed_at TIMESTAMPTZ, healthy_at TIMESTAMPTZ, catalog_fingerprint TEXT,
               status TEXT NOT NULL, generation BIGINT NOT NULL,
               UNIQUE(source_id,label))""",
        )
        for sql in ordered:
            await self.db.execute_raw(sql)

    async def source(self, source_id: str) -> SourceRecord | None:
        rows: Final = await self._sources(
            self.db, "SELECT " + SOURCE_COLUMNS + ' FROM "OicmMcpSource" WHERE source_id=$1', source_id
        )
        return rows[0] if rows else None

    async def enroll(self, value: SourceCreate, owner: str) -> SourceRecord | NotOwnerConflict | ContentConflict:
        if value.owner != owner:
            raise PermissionDenied("source owner mismatch")
        sid: Final = value.source_id_for(owner, value.alias)
        grant: Final = self.keyring.enrollment_id(sid)
        async with self.db.tx(timeout=10000) as tx:
            await tx.execute_raw("SELECT pg_advisory_xact_lock(hashtext($1))", f"owner:{owner}:{value.alias}")
            existing: Final = await tx.query_raw(
                "SELECT " + SOURCE_COLUMNS + ' FROM "OicmMcpSource" WHERE source_id=$1 FOR UPDATE', sid
            )
            found: Final = tuple(row.value() for row in SOURCES.validate_python(existing))
            if found and found[0].expected == value.fingerprint() and found[0].target == value.target:
                return found[0]
            if found and found[0].tenant != owner:
                return NotOwnerConflict(id=sid)
            if found:
                # An identical re-enroll is idempotent and returns the stored row.
                # Anything else is changed content, and it must not touch the row:
                # rewriting target/fingerprint here would repoint an approved
                # source's artifact before the gateway ever made its conflict
                # decision, so a pending retry could silently swap what an
                # approved row serves.
                return ContentConflict(id=sid)
            row: Final = SQLRow(
                source_id=sid,
                tenant=owner,
                key=value.identity(),
                alias=value.alias,
                target_host=value.target.host,
                target_port=value.target.port,
                spec_path=value.spec_path,
                expected=value.fingerprint(),
                gateway_auth_ciphertext=self.keyring.gateway_ciphertext(value.identity()),
                # Connectivity lifecycle, not approval: "pending" means enrolled
                # but not yet probed healthy, and remote_store.report() promotes
                # it to "active" on a good probe. Approval lives only on the
                # gateway row's approval_status.
                status="pending",
                revision=1,
            )
            await tx.execute_raw(
                'INSERT INTO "OicmMcpSource" (' + SOURCE_COLUMNS + ") VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)",
                row.source_id,
                row.tenant,
                row.key,
                row.alias,
                row.target_host,
                row.target_port,
                row.spec_path,
                row.expected,
                row.gateway_auth_ciphertext,
                row.status,
                row.revision,
            )
            # The source's enrollment row is the anchor every worker row hangs
            # off (OicmMcpRemoteWorker.enrollment_id is a foreign key), so it is
            # written even though nothing redeems it any more. The expiry is
            # server-derived because the connector no longer supplies one.
            await tx.execute_raw(
                'INSERT INTO "OicmMcpEnrollment" (id,source_id,token_hash,expires_at,redeemed_at) '
                "VALUES ($1,$2,$3::bytea,$4::timestamptz,NULL) ON CONFLICT (id) DO NOTHING",
                grant,
                sid,
                "\\x" + self.keyring.token_hash(grant, self.keyring.mint_token()).hex(),
                value.expires_at or datetime.now(timezone.utc) + timedelta(days=3650),
            )
            return row.value()
