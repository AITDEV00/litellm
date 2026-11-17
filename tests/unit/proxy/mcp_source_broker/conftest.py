from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from litellm.proxy.mcp_source_broker.keys import Keys
from litellm.proxy.mcp_source_broker.models import SourceCreate, Target
from litellm.proxy.mcp_source_broker.store import SQLRow, SourceStore


class MemorySource(SQLRow):
    remote_transport: str = "remotedialer"


class MemoryDatabase:
    def __init__(self) -> None:
        self.sources: list[MemorySource] = []
        self.locks: list[str] = []
        self.enrollments: list[str] = []
        self.now = datetime.now(timezone.utc)
        self.timeouts: list[int] = []

    def tx(self, *, timeout: int = 5000):
        self.timeouts.append(timeout)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, kind: object, error: object, tb: object | None) -> None:
        return None

    @staticmethod
    def binary(value: object) -> bytes:
        if isinstance(value, str) and value.startswith("\\x"):
            return bytes.fromhex(value[2:])
        raise AssertionError("expected the raw bytea literal representation")

    async def query_raw(self, sql: str, *args: object):
        if "SELECT pg_advisory_xact_lock" in sql:
            return []
        if "SELECT remote_transport" in sql:
            return [{"remote_transport": s.remote_transport} for s in self.sources if s.source_id == str(args[0])]
        if 'FROM "OicmMcpSource" WHERE source_id=$1' in sql:
            return [s.model_dump() for s in self.sources if s.source_id == str(args[0])]
        raise AssertionError(sql[:100])

    async def execute_raw(self, sql: str, *args: object) -> int:
        if (
            "advisory_xact_lock" in sql
            or "LOCK TABLE" in sql
            or "CREATE TABLE" in sql
            or "ADD COLUMN IF NOT EXISTS" in sql
        ):
            self.locks.append(sql)
            return 0
        if 'INSERT INTO "OicmMcpEnrollment"' in sql:
            assert "$4::timestamptz" in sql, (
                "expires_at must be cast for timestamptz; text binding is rejected by PostgreSQL"
            )
            assert args[1] == str(args[1])
            self.enrollments.append(str(args[0]))
            return 1
        if 'INSERT INTO "OicmMcpSource"' in sql:
            self.sources.append(
                MemorySource.model_validate(
                    dict(
                        zip(
                            (
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
                            ),
                            args,
                        )
                    )
                )
            )
            return 1
        if 'UPDATE "OicmMcpSource" SET target_host=' in sql:
            for index, row in enumerate(self.sources):
                if row.source_id == str(args[0]):
                    self.sources[index] = row.model_copy(
                        update={
                            "target_host": str(args[1]),
                            "target_port": int(args[2]),
                            "expected": str(args[3]),
                            "status": "pending",
                        }
                    )
            return 1
        if 'UPDATE "OicmMcpSource" SET remote_transport' in sql:
            for index, row in enumerate(self.sources):
                if row.source_id == str(args[0]) and row.remote_transport != str(args[1]):
                    self.sources[index] = row.model_copy(
                        update={"revision": row.revision + 1, "remote_transport": str(args[1])}
                    )
            return 1
        raise AssertionError(sql[:100])


@pytest.fixture
def store() -> SourceStore:
    db = MemoryDatabase()
    wrapped = SourceStore(db, Keys("p" * 32))
    wrapped.db = db  # pyright: ignore[reportAttributeAccessIssue]  # test fixture exposes the memory fake
    return wrapped


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
