from __future__ import annotations

import asyncio
import base64
import hmac
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType, TracebackType
from typing import Final, Literal, Protocol
from uuid import uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator
from typing_extensions import LiteralString

from litellm.proxy.mcp_source_broker.keys import Keys
from litellm.proxy.mcp_source_broker.models import (
    NAMESPACE,
    Claim,
    Enrollment,
    EnrollmentRow,
    Member,
    MemberView,
    RelayMember,
    SourceCreate,
    SourceRecord,
    Target,
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

    @classmethod
    def from_value(cls, row: SourceRecord) -> SQLRow:
        return cls(
            source_id=row.source_id,
            tenant=row.tenant,
            key=row.key,
            alias=row.alias,
            target_host=row.target.host,
            target_port=row.target.port,
            spec_path=row.spec_path,
            expected=row.expected,
            gateway_auth_ciphertext=row.gateway_auth_ciphertext,
            status=row.status,
            revision=row.revision,
        )


SOURCES: Final = TypeAdapter(tuple[SQLRow, ...])
ENROLLMENTS: Final = TypeAdapter(tuple[EnrollmentRow, ...])
MEMBERS: Final = TypeAdapter(tuple[Member, ...])


class OwnerRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    tenant: str


class LeaseRow(BaseModel):
    model_config = ConfigDict(frozen=True)
    host_number: int = Field(ge=20, le=254)
    port_number: int = Field(ge=18080, le=18120)


OWNERS: Final = TypeAdapter(tuple[OwnerRow, ...])
LEASES: Final = TypeAdapter(tuple[LeaseRow, ...])
SOURCE_COLUMNS: Final = (
    "source_id,tenant,key,alias,target_host,target_port,spec_path,expected,gateway_auth_ciphertext,status,revision"
)


@dataclass(frozen=True, slots=True)
class NotOwner:
    pass


@dataclass(frozen=True, slots=True)
class NotOwnerConflict:
    id: str


class NoLease(Exception):
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


class UserMembership(BaseModel):
    model_config = ConfigDict(frozen=True)
    member: Member
    enrollment: EnrollmentRow
    source: SQLRow

    @field_validator("member", "enrollment", mode="before")
    @classmethod
    def decode_postgres_bytes_and_time(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        return postgres_json_row(value)

    @property
    def source_value(self) -> SourceRecord:
        return self.source.value()


MEMBERSHIPS: Final = TypeAdapter(tuple[UserMembership, ...])


class SourceStore:
    def __init__(self, db: Database, keyring: Keys) -> None:
        self.db: Final = db
        self.keyring: Final = keyring

    async def _sources(self, db: SQLDatabase, query: LiteralString, *args: object) -> Sequence[SourceRecord]:
        return tuple(row.value() for row in SOURCES.validate_python(await db.query_raw(query, *args)))

    async def _members(self, query: LiteralString, *args: object, db: SQLDatabase | None = None) -> Sequence[Member]:
        rows: Final = TypeAdapter(tuple[dict[str, object], ...]).validate_python(
            await (db or self.db).query_raw(query, *args)
        )
        return MEMBERS.validate_python(tuple(postgres_json_row(row) for row in rows))

    async def owner_of_enrollment(self, grant_id: str) -> str | None:
        rows: Final = OWNERS.validate_python(
            await self.db.query_raw(
                'SELECT s.tenant FROM "OicmMcpEnrollment" e JOIN "OicmMcpSource" s ON s.source_id=e.source_id '
                "WHERE e.id=$1",
                grant_id,
            )
        )
        return rows[0].tenant if rows else None

    async def ensure(self) -> None:
        statements: Final = (
            """CREATE TABLE IF NOT EXISTS "OicmMcpSource" (source_id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
               key TEXT NOT NULL, alias TEXT NOT NULL, target_host TEXT NOT NULL, target_port INT NOT NULL,
               spec_path TEXT NOT NULL, expected TEXT NOT NULL, gateway_auth_ciphertext TEXT NOT NULL,
               status TEXT NOT NULL, revision BIGINT NOT NULL, CONSTRAINT source_owner_alias UNIQUE (tenant,alias))""",
            """CREATE TABLE IF NOT EXISTS "OicmMcpEnrollment" (id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE
               REFERENCES "OicmMcpSource"(source_id), token_hash BYTEA NOT NULL,
               expires_at TIMESTAMPTZ NOT NULL, redeemed_at TIMESTAMPTZ)""",
            """CREATE TABLE IF NOT EXISTS "OicmMcpConnector" (connector_id TEXT PRIMARY KEY, source_id TEXT NOT NULL
               REFERENCES "OicmMcpSource"(source_id), enrollment_id TEXT NOT NULL REFERENCES "OicmMcpEnrollment"(id),
               relay_host TEXT NOT NULL, relay_port INT NOT NULL, credential_digest BYTEA NOT NULL,
               healthy_at TIMESTAMPTZ, catalog_fingerprint TEXT, status TEXT NOT NULL, generation BIGINT NOT NULL,
               UNIQUE(source_id,connector_id), UNIQUE(relay_host,relay_port))""",
        )
        await asyncio.gather(*(self.db.execute_raw(sql) for sql in statements))

    async def source(self, source_id: str) -> SourceRecord | None:
        rows: Final = await self._sources(
            self.db, "SELECT " + SOURCE_COLUMNS + ' FROM "OicmMcpSource" WHERE source_id=$1', source_id
        )
        return rows[0] if rows else None

    async def enroll(self, value: SourceCreate, owner: str, key: str) -> Enrollment | NotOwner | NotOwnerConflict:
        if value.owner != owner:
            return NotOwner()
        sid: Final = value.source_id_for(owner, value.alias)
        grant: Final = self.keyring.enrollment_id(sid)
        async with self.db.tx(timeout=10000) as tx:
            await tx.execute_raw("SELECT pg_advisory_xact_lock(hashtext($1))", sid)
            existing: Final = await self._sources(
                tx,
                "SELECT "
                + SOURCE_COLUMNS
                + ' FROM "OicmMcpSource" WHERE (tenant=$1 AND alias=$2) OR source_id=$3 FOR UPDATE',
                owner,
                value.alias,
                sid,
            )
            if existing:
                found: Final = existing[0]
                if (
                    found.source_id == sid
                    and found.tenant == owner
                    and hmac.compare_digest(found.key, value.identity())
                ):
                    await tx.execute_raw(
                        'UPDATE "OicmMcpSource" SET target_host=$1,target_port=$2,expected=$3,'
                        "status='pending',revision=revision+1 WHERE source_id=$4 AND (target_host,target_port,expected) "
                        "IS DISTINCT FROM ($1,$2,$3)",
                        value.target.host,
                        value.target.port,
                        value.fingerprint(),
                        sid,
                    )
                    return Enrollment(
                        source=found.model_copy(
                            update={
                                "target": value.target,
                                "expected": value.fingerprint(),
                                "status": "pending" if found.expected != value.fingerprint() else found.status,
                            }
                        ),
                        enrollment_id=grant,
                        token=None,
                    )
                return NotOwnerConflict(found.source_id)
            token: Final = self.keyring.mint_token()
            created: Final = Enrollment.create(owner, value, grant, token).source.model_copy(
                update={"gateway_auth_ciphertext": self.keyring.gateway_ciphertext(key)}
            )
            row: Final = SQLRow.from_value(created)
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
            await tx.execute_raw(
                'INSERT INTO "OicmMcpEnrollment" (id,source_id,token_hash,expires_at,redeemed_at) VALUES ($1,$2,$3::bytea,$4::timestamptz,NULL)',
                grant,
                sid,
                "\\x" + self.keyring.token_hash(grant, token).hex(),
                value.expires_at,
            )
            return Enrollment(source=created, enrollment_id=grant, token=token)

    def _claim(self, source: SourceRecord, member: Member, grant: str) -> Claim:
        return Claim(
            source=source,
            member=MemberView(
                connector_id=member.connector_id,
                source_id=member.source_id,
                relay_host=member.relay_host,
                relay_port=member.relay_port,
                healthy_at=member.healthy_at,
                catalog_fingerprint=member.catalog_fingerprint,
                status=member.status,
                generation=member.generation,
            ),
            username=self.keyring.username(member.connector_id),
            password=self.keyring.password(grant, member.connector_id),
        )

    async def claim(self, grant_id: str, token: str | None) -> Claim | None:
        if token is None:
            return None
        async with self.db.tx(timeout=10000) as tx:
            grants: Final = ENROLLMENTS.validate_python(
                await tx.query_raw(
                    'UPDATE "OicmMcpEnrollment" SET redeemed_at=NOW() WHERE id=$1 AND token_hash=$2::bytea '
                    "AND redeemed_at IS NULL AND expires_at>NOW() RETURNING id,source_id,token_hash,expires_at,redeemed_at",
                    grant_id,
                    "\\x" + self.keyring.token_hash(grant_id, token).hex(),
                )
            )
            if not grants:
                return None
            sources: Final = await self._sources(
                tx,
                "SELECT " + SOURCE_COLUMNS + ' FROM "OicmMcpSource" WHERE source_id=$1 FOR UPDATE',
                grants[0].source_id,
            )
            if not sources or sources[0].status not in {"pending", "active"}:
                raise ValueError("claimable source not found")
            found: Final = sources[0]
            await tx.execute_raw('LOCK TABLE "OicmMcpConnector" IN SHARE ROW EXCLUSIVE MODE')
            leases: Final = await self._allocate_lease(tx)
            if not leases:
                raise NoLease()
            connector: Final = str(uuid5(NAMESPACE, f"connector:{found.source_id}:{grant_id}"))
            host: Final = f"{leases[0][0]}"
            port: Final = leases[0][1]
            await tx.execute_raw(
                'INSERT INTO "OicmMcpConnector" (connector_id,source_id,enrollment_id,relay_host,'
                "relay_port,credential_digest,healthy_at,catalog_fingerprint,status,generation) VALUES ($1,$2,$3,$4,$5,$6::bytea,NULL,NULL,$7,$8)",
                connector,
                found.source_id,
                grant_id,
                host,
                port,
                "\\x" + self.keyring.digest(grant_id, connector).hex(),
                "pending",
                1,
            )
            member: Final = Member(
                connector_id=connector,
                source_id=found.source_id,
                relay_host=host,
                relay_port=port,
                credential_digest=self.keyring.digest(grant_id, connector),
                healthy_at=None,
                catalog_fingerprint=None,
                status="pending",
                generation=1,
            )
            return self._claim(found, member, grant_id)

    async def _allocate_lease(self, tx: SQLDatabase) -> Sequence[tuple[str, int]]:
        rows: Final = await tx.query_raw(
            "SELECT h.host_number,p.port_number FROM generate_series(20,254) AS h(host_number) "
            "CROSS JOIN generate_series(18080,18120) AS p(port_number) "
            "LEFT JOIN \"OicmMcpConnector\" c ON c.relay_host=('127.0.0.'||h.host_number) AND c.relay_port=p.port_number "
            "WHERE c.connector_id IS NULL ORDER BY h.host_number,p.port_number LIMIT 1"
        )
        return tuple((f"127.0.0.{lease.host_number}", lease.port_number) for lease in LEASES.validate_python(rows))

    async def get_claim_if_user(self, grant_id: str) -> Claim | None:
        async with self.db.tx(timeout=10000) as tx:
            members: Final = await self._members(
                "SELECT connector_id,source_id,relay_host,relay_port,credential_digest,healthy_at,"
                'catalog_fingerprint,status,generation FROM "OicmMcpConnector" WHERE enrollment_id=$1 FOR UPDATE',
                grant_id,
                db=tx,
            )
            if not members:
                return None
            sources: Final = await self._sources(
                tx,
                "SELECT " + SOURCE_COLUMNS + ' FROM "OicmMcpSource" WHERE source_id=$1 FOR SHARE',
                members[0].source_id,
            )
            if not sources:
                return None
            return self._claim(sources[0], members[0], grant_id)

    async def users(self) -> Sequence[RelayMember]:
        rows: Final = MEMBERSHIPS.validate_python(
            await self.db.query_raw(
                "SELECT row_to_json(c.*) AS member, row_to_json(e.*) AS enrollment, row_to_json(s.*) AS source "
                'FROM "OicmMcpConnector" c JOIN "OicmMcpEnrollment" e ON e.id=c.enrollment_id '
                'JOIN "OicmMcpSource" s ON s.source_id=c.source_id ORDER BY c.connector_id'
            )
        )
        return tuple(
            RelayMember(
                connector_id=row.member.connector_id,
                source_id=row.member.source_id,
                relay_host=row.member.relay_host,
                relay_port=row.member.relay_port,
                username=self.keyring.username(row.member.connector_id),
                password=self.keyring.password(row.enrollment.id, row.member.connector_id),
                target_port=row.source.target_port,
                status=row.member.status,
                generation=row.member.generation,
                enrollment_id=row.enrollment.id,
            )
            for row in rows
        )

    async def healthy_members(self, source_id: str, ttl: timedelta) -> Sequence[Member]:
        return await self._members(
            "SELECT connector_id,source_id,relay_host,relay_port,credential_digest,healthy_at,"
            'catalog_fingerprint,c.status,generation FROM "OicmMcpConnector" c JOIN "OicmMcpSource" s USING(source_id) '
            "WHERE c.source_id=$1 AND c.status='active' AND c.healthy_at>=$2::timestamptz "
            "AND c.catalog_fingerprint=s.expected AND s.status='active' "
            "ORDER BY connector_id",
            source_id,
            (datetime.now(timezone.utc) - ttl).isoformat(),
        )

    async def probe_connector(
        self, connector_id: str, source_id: str, digest: bytes, fingerprint: str | None
    ) -> Member | None:
        members: Final = await self._members(
            "SELECT connector_id,source_id,relay_host,relay_port,credential_digest,healthy_at,"
            'catalog_fingerprint,status,generation FROM "OicmMcpConnector" WHERE connector_id=$1 AND source_id=$2',
            connector_id,
            source_id,
        )
        found: Final = await self.source(source_id)
        if not members or found is None:
            return None
        authenticated: Final = hmac.compare_digest(members[0].credential_digest, digest)
        valid_spec: Final = fingerprint is not None
        good: Final = authenticated and valid_spec and hmac.compare_digest(found.expected, fingerprint)
        await self.db.execute_raw(
            'UPDATE "OicmMcpConnector" SET healthy_at=CASE WHEN $1 THEN NOW() ELSE NULL END,'
            "catalog_fingerprint=CASE WHEN $1 THEN $2 ELSE NULL END,status=CASE WHEN $1 THEN 'active' ELSE 'quarantine' END "
            "WHERE connector_id=$3",
            good,
            fingerprint,
            connector_id,
        )
        if good or (authenticated and valid_spec):
            await self.db.execute_raw(
                'UPDATE "OicmMcpSource" SET status=$2,revision=revision+1 WHERE source_id=$1 '
                "AND (status IN ('pending','active') OR (status='quarantined' AND $3))",
                source_id,
                "active" if good else "quarantined",
                good,
            )
        return members[0].model_copy(
            update={
                "healthy_at": datetime.now(timezone.utc) if good else None,
                "catalog_fingerprint": fingerprint if good else None,
                "status": "active" if good else "quarantine",
            }
        )
