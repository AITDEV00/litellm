from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import TracebackType

import pytest
from prisma.fields import Base64

from litellm.proxy.mcp_source_broker.keys import Keys
from litellm.proxy.mcp_source_broker.models import EnrollmentRow, Member, RelayConfig, SourceCreate, Target
from litellm.proxy.mcp_source_broker.store import SQLRow, SourceStore


@dataclass
class MemoryTables:
    sources: list[SQLRow]
    grants: list[EnrollmentRow]
    members: list[Member]


class MemoryDatabase:
    def __init__(self) -> None:
        self.state = MemoryTables([], [], [])
        self.locks: list[str] = []
        self.now = datetime.now(timezone.utc)
        self.timeouts: list[int] = []

    def tx(self, *, timeout: int = 5000):
        self.timeouts.append(timeout)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind: object, error: object, tb: TracebackType | None) -> None:
        return None

    @staticmethod
    def binary(value: object) -> bytes:
        if isinstance(value, str) and value.startswith("\\x"):
            return bytes.fromhex(value[2:])
        raise AssertionError("expected the raw bytea literal representation")

    async def query_raw(self, sql: str, *args: object):
        if "SELECT s.tenant" in sql:
            return [
                {"tenant": s.tenant}
                for s in self.state.sources
                if any(g.id == args[0] and g.source_id == s.source_id for g in self.state.grants)
            ]
        if 'UPDATE "OicmMcpEnrollment"' in sql:
            grant = next(
                (
                    g
                    for g in self.state.grants
                    if g.id == args[0]
                    and g.token_hash == self.binary(args[1])
                    and g.redeemed_at is None
                    and g.expires_at > self.now
                ),
                None,
            )
            if grant is None:
                return []
            updated = grant.model_copy(update={"redeemed_at": self.now})
            self.state.grants[self.state.grants.index(grant)] = updated
            return [updated.model_dump()]
        if "generate_series" in sql:
            used = {(m.relay_host, m.relay_port) for m in self.state.members}
            host, port = next(
                (f"127.0.0.{i}", p)
                for i in range(20, 255)
                for p in range(18080, 18121)
                if (f"127.0.0.{i}", p) not in used
            )
            return [{"host_number": int(host.rsplit(".", 1)[1]), "port_number": port}]
        if "AS source" in sql and "AS enrollment" in sql:
            return [
                {
                    "member": {
                        **m.model_dump(),
                        "credential_digest": "\\x" + m.credential_digest.hex(),
                        "healthy_at": m.healthy_at.isoformat() if m.healthy_at else None,
                    },
                    "enrollment": {
                        key: (
                            value.isoformat()
                            if isinstance(value, datetime)
                            else "\\x" + value.hex()
                            if key.endswith("_hash")
                            else value
                        )
                        for key, value in next(
                            g.model_dump() for g in self.state.grants if g.source_id == m.source_id
                        ).items()
                    },
                    "source": next(s.model_dump() for s in self.state.sources if s.source_id == m.source_id),
                }
                for m in self.state.members
                if any(s.source_id == m.source_id for s in self.state.sources)
            ]
        if 'FROM "OicmMcpConnector" c JOIN "OicmMcpSource" s USING(source_id)' in sql:
            found = next((s for s in self.state.sources if s.source_id == args[0]), None)
            return [
                m.model_dump()
                for m in self.state.members
                if found is not None
                and found.status == "active"
                and m.catalog_fingerprint == found.expected
                and m.status == "active"
                and m.healthy_at is not None
                and m.healthy_at.isoformat() >= args[1]
            ]
        if "WHERE enrollment_id" in sql:
            return [
                m.model_dump()
                for m in self.state.members
                if m.source_id in {g.source_id for g in self.state.grants if g.id == args[0]}
            ]
        if "WHERE connector_id" in sql:
            return [m.model_dump() for m in self.state.members if m.connector_id == args[0]]
        if "FOR UPDATE" in sql and len(args) == 3:
            return [
                s.model_dump()
                for s in self.state.sources
                if (s.tenant == args[0] and s.alias == args[1]) or s.source_id == args[2]
            ]
        if "WHERE source_id" in sql:
            return [s.model_dump() for s in self.state.sources if s.source_id == args[0]]
        raise AssertionError(sql[:100])

    async def execute_raw(self, sql: str, *args: object) -> int:
        if "advisory_xact_lock" in sql or "LOCK TABLE" in sql or "CREATE TABLE" in sql:
            self.locks.append(sql)
            return 0
        if "SET target_host=" in sql:
            for index, source_row in enumerate(self.state.sources):
                if source_row.source_id == args[3]:
                    self.state.sources[index] = source_row.model_copy(
                        update={
                            "target": Target(host=str(args[0]), port=int(args[1])),
                            "expected": str(args[2]),
                            "status": "pending",
                        }
                    )
            return 1
        if 'UPDATE "OicmMcpSource"' in sql:
            for index, source_row in enumerate(self.state.sources):
                if source_row.source_id == args[0] and (
                    source_row.status in {"pending", "active"} or (source_row.status == "quarantined" and args[2])
                ):
                    self.state.sources[index] = source_row.model_copy(update={"status": args[1]})
            return 1
        if 'INSERT INTO "OicmMcpSource"' in sql:
            assert all(s.source_id != args[0] for s in self.state.sources)
            words = (
                "source_id",
                "tenant",
                "key",
                "alias",
                "target_host",
                "target_port",
                "spec_path",
                "expected",
                "gateway_auth_ciphertext",
                "status",
                "revision",
            )
            self.state.sources.append(SQLRow.model_validate(dict(zip(words, args))))
            return 1
        if 'INSERT INTO "OicmMcpEnrollment"' in sql:
            self.state.grants.append(
                EnrollmentRow(
                    id=str(args[0]),
                    source_id=str(args[1]),
                    token_hash=self.binary(args[2]),
                    expires_at=args[3],
                    redeemed_at=None,
                )
            )
            return 1
        if 'INSERT INTO "OicmMcpConnector"' in sql:
            self.state.members.append(
                Member(
                    connector_id=str(args[0]),
                    source_id=str(args[1]),
                    relay_host=str(args[3]),
                    relay_port=int(args[4]),
                    credential_digest=self.binary(args[5]),
                    healthy_at=None,
                    catalog_fingerprint=None,
                    status=str(args[6]),
                    generation=int(args[7]),
                )
            )
            return 1
        if 'UPDATE "OicmMcpConnector"' in sql:
            idx = next(i for i, m in enumerate(self.state.members) if m.connector_id == args[2])
            self.state.members[idx] = self.state.members[idx].model_copy(
                update={
                    "healthy_at": self.now if args[0] else None,
                    "catalog_fingerprint": args[1] if args[0] else None,
                    "status": "active" if args[0] else "quarantine",
                }
            )
            return 1
        raise AssertionError(sql[:100])


@pytest.fixture
def store() -> SourceStore:
    return SourceStore(MemoryDatabase(), Keys("p" * 32))


@pytest.fixture
def source() -> SourceCreate:
    return SourceCreate(
        owner="alice",
        alias="pets-api",
        key="another-source-key",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        target=Target(),
        spec='{"openapi":"3.1.0","paths":{"/v1/pets":{"get":{}}}}',
    )


@pytest.fixture
def settings() -> RelayConfig:
    return RelayConfig(
        relay_url="https://relay.example/mcp-worker",
        public_url="https://gateway.example",
        fingerprint="a" * 64,
        chisel_sha256="b" * 64,
    )
