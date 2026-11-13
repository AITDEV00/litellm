import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError
from prisma.fields import Base64

from litellm.proxy.mcp_source_broker.keys import Keys
from litellm.proxy.mcp_source_broker.models import Member, SourceCreate, Target
from litellm.proxy.mcp_source_broker.store import NotOwner, NoLease, NotOwnerConflict, SourceStore


@pytest.mark.asyncio
async def test_enroll_no_plaintext_one_claim_and_recovery(store: SourceStore, source: SourceCreate) -> None:
    await store.ensure()
    result = await store.enroll(source, source.owner, "catalog-encryption-key")
    assert result.source.source_id == source.source_id and len(result.token) >= 32
    assert result.source.gateway_auth_ciphertext != "catalog-encryption-key"
    db = store.db
    assert len(db.state.sources) == 1 and db.state.sources[0].tenant == "alice"
    assert db.state.sources[0].key == source.identity()
    assert isinstance(await store.enroll(source, "bob", "k"), NotOwner)
    assert isinstance(
        await store.enroll(source.model_copy(update={"key": "different-key-material"}), source.owner, "k"),
        NotOwnerConflict,
    )
    repeat = await store.enroll(source, source.owner, "catalog-encryption-key")
    assert repeat.token is None and len(db.state.grants) == 1
    claim = await store.claim(result.enrollment_id, result.token)
    assert claim is not None and (claim.member.relay_host, claim.member.relay_port) == ("127.0.0.20", 18080)
    assert claim.password == store.keyring.password(result.enrollment_id, claim.member.connector_id)
    assert await store.claim(result.enrollment_id, result.token) is None
    recovered = await store.get_claim_if_user(result.enrollment_id)
    assert recovered is not None and recovered.member == claim.member and recovered.password == claim.password
    users = await store.users()
    assert [(u.password, u.target_port, u.status) for u in users] == [(claim.password, 18080, "pending")]
    assert users[0].enrollment_id == result.enrollment_id
    assert sum("CREATE TABLE IF NOT EXISTS" in sql for sql in db.locks) == 4
    assert any("ADD COLUMN IF NOT EXISTS remote_transport" in sql for sql in db.locks)
    assert any("advisory_xact_lock" in sql for sql in db.locks)
    assert sum("SHARE ROW EXCLUSIVE" in sql for sql in db.locks) == 1
    assert await store.owner_of_enrollment(result.enrollment_id) == "alice"
    assert await store.owner_of_enrollment("missing") is None
    assert db.timeouts and set(db.timeouts) == {10000}


@pytest.mark.asyncio
async def test_expired_cannot_consume_member(store: SourceStore, source: SourceCreate) -> None:
    attempt = await store.enroll(
        source.model_copy(update={"expires_at": datetime.now(timezone.utc) - timedelta(1)}), source.owner, "key"
    )
    assert await store.claim(attempt.enrollment_id, attempt.token) is None
    assert store.db.state.members == []
    assert await store.claim(attempt.enrollment_id, attempt.token + "bad") is None


@pytest.mark.asyncio
async def test_probe_health_freshness_and_foreign_digest(store: SourceStore, source: SourceCreate) -> None:
    enrolled = await store.enroll(source, source.owner, "key")
    claim = await store.claim(enrolled.enrollment_id, enrolled.token)
    assert claim is not None
    digest = store.db.state.members[0].credential_digest
    assert await store.healthy_members(source.source_id, timedelta(seconds=30)) == ()
    mismatched = await store.probe_connector(claim.member.connector_id, source.source_id, digest, "0" * 64)
    assert mismatched is not None and mismatched.status == "quarantine"
    users = await store.users()
    assert len(users) == 1 and users[0].status == "quarantine"
    recovered = await store.get_claim_if_user(enrolled.enrollment_id)
    assert recovered is not None and recovered.password == claim.password
    assert await store.healthy_members(source.source_id, timedelta(seconds=30)) == ()
    assert isinstance(
        await store.probe_connector(claim.member.connector_id, source.source_id, bytes(32), source.fingerprint()),
        Member,
    )
    valid = await store.probe_connector(claim.member.connector_id, source.source_id, digest, source.fingerprint())
    assert valid is not None and valid.status == "active"
    store.db.state.members[0] = store.db.state.members[0].model_copy(
        update={"credential_digest": Base64.encode(digest).decode()}
    )
    assert (
        await store.probe_connector(claim.member.connector_id, source.source_id, digest, source.fingerprint())
    ) is not None
    db_members = await store.healthy_members(source.source_id, timedelta(seconds=45))
    assert len(db_members) == 1 and db_members[0].connector_id == valid.connector_id
    assert db_members[0].healthy_at == store.db.now and db_members[0].catalog_fingerprint == source.fingerprint()
    assert await store.healthy_members(source.source_id, timedelta(seconds=0)) == ()
    source_row = store.db.state.sources[0].model_copy(update={"status": "quarantined"})
    store.db.state.sources[0] = source_row
    assert len(await store.users()) == 1
    user_claim = await store.get_claim_if_user(enrolled.enrollment_id)
    assert user_claim is not None and user_claim.member.connector_id == claim.member.connector_id
    assert await store.healthy_members(source.source_id, timedelta(seconds=45)) == ()
    recovered = await store.probe_connector(claim.member.connector_id, source.source_id, digest, source.fingerprint())
    assert recovered.status == "active"
    assert store.db.state.sources[0].status == "active"


@pytest.mark.skipif("LITELLM_TEST_POSTGRES" not in os.environ, reason="requires a disposable test PostgreSQL")
@pytest.mark.asyncio
async def test_postgres_member_health_queries_use_typed_params() -> None:
    from prisma import Prisma

    db = Prisma(datasource={"url": os.environ["LITELLM_TEST_POSTGRES"]})

    class QueryRecorder:
        def __init__(self) -> None:
            self.queries: list[str] = []

        async def execute_raw(self, query: str, *args: object) -> int:
            return await db.execute_raw(query, *args)

        def tx(self, *, timeout: int):
            return db.tx(timeout=timeout)

        async def query_raw(self, query: str, *args: object) -> object:
            self.queries.append(query)
            return await db.query_raw(query, *args)

    recorder = QueryRecorder()
    await db.connect()
    store = SourceStore(recorder, Keys("integration-pepper-not-persisted-123456"))
    await store.ensure()
    found = SourceCreate(
        owner="postgres-regression",
        alias=f"postgres-regression-{uuid.uuid4().hex[:12]}",
        key="integration-source-key-unique",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        target=Target(),
        spec='{"openapi":"3.1.0","paths":{"/v1/pets":{"get":{}}}}',
    )
    try:
        enrolled = await store.enroll(found, found.owner, found.key)
        assert (await store.enroll(found, found.owner, found.key)).enrollment_id == enrolled.enrollment_id
        assert enrolled.token is not None
        claimed = await store.claim(enrolled.enrollment_id, enrolled.token)
        assert claimed is not None
        member = await store.probe_connector(
            claimed.member.connector_id,
            found.source_id,
            store.keyring.digest(enrolled.enrollment_id, claimed.member.connector_id),
            found.fingerprint(),
        )
        assert member is not None and member.status == "active"
        assert len(await store.healthy_members(found.source_id, timedelta(seconds=30))) == 1
        users = await store.users()
        assert [user.connector_id for user in users if user.source_id == found.source_id] == [
            claimed.member.connector_id
        ]
        mismatch = "0" * 64
        quarantined = await store.probe_connector(
            claimed.member.connector_id,
            found.source_id,
            store.keyring.digest(enrolled.enrollment_id, claimed.member.connector_id),
            mismatch,
        )
        assert quarantined is not None and quarantined.status == "quarantine"
        assert await store.healthy_members(found.source_id, timedelta(seconds=30)) == ()
        assert any("c.healthy_at>=$2::timestamptz" in q for q in recorder.queries)
        assert await store.get_claim_if_user(enrolled.enrollment_id) is not None
        assert (
            await store.probe_connector(
                claimed.member.connector_id,
                found.source_id,
                store.keyring.digest(enrolled.enrollment_id, claimed.member.connector_id),
                found.fingerprint(),
            )
        ).status == "active"
        assert len(await store.healthy_members(found.source_id, timedelta(seconds=30))) == 1
    finally:
        await db.execute_raw('DELETE FROM "OicmMcpConnector" WHERE source_id=$1', found.source_id)
        await db.execute_raw('DELETE FROM "OicmMcpEnrollment" WHERE source_id=$1', found.source_id)
        await db.execute_raw('DELETE FROM "OicmMcpSource" WHERE tenant=$1', found.owner)
        await db.disconnect()


@pytest.mark.parametrize("value", ["-bad", "a", "Contains_upper"])
def test_invalid_alias_rejected(source: SourceCreate, value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(SourceCreate).validate_python({**source.model_dump(), "alias": value})


def test_loopback_required() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(Target).validate_python({"host": "192.168.0.1"})


def test_canonical_catalog_is_bounded(source: SourceCreate) -> None:
    import json

    with pytest.raises(ValidationError):
        SourceCreate(**{**source.model_dump(), "spec": json.dumps({"openapi": "3.0.0", "paths": {"x": "x" * 500_000}})})
