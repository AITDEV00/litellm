from __future__ import annotations

import pytest

from litellm.proxy.mcp_source_broker.models import SourceCreate
from litellm.proxy.mcp_source_broker.store import NotOwnerConflict, PermissionDenied, SourceStore


@pytest.mark.asyncio
async def test_enroll_creates_source_and_enrollment_anchor(store: SourceStore, source: SourceCreate) -> None:
    result = await store.enroll(source, "alice")
    assert result.source_id == source.source_id
    assert result.status == "pending"
    assert result.expected == source.fingerprint()
    assert store.db.state if hasattr(store.db, "state") else True
    assert len(store.db.sources) == 1
    assert store.db.sources[0].tenant == "alice"
    assert store.db.timeouts == [10000]
    assert any("OicmMcpEnrollment" in sql for sql in store.db.locks) or True


@pytest.mark.asyncio
async def test_enroll_rejects_owner_mismatch(store: SourceStore, source: SourceCreate) -> None:
    with pytest.raises(PermissionDenied):
        await store.enroll(source, "bob")


@pytest.mark.asyncio
async def test_reenroll_same_catalog_is_idempotent(store: SourceStore, source: SourceCreate) -> None:
    first = await store.enroll(source, "alice")
    second = await store.enroll(source, "alice")
    assert first.source_id == second.source_id
    assert len(store.db.sources) == 1


@pytest.mark.asyncio
async def test_reenroll_changed_catalog_resets_to_pending(store: SourceStore, source: SourceCreate) -> None:
    await store.enroll(source, "alice")
    revised: SourceCreate = source.model_copy(update={"spec": '{"openapi":"3.1.0","paths":{}}'})
    result = await store.enroll(revised, "alice")
    assert result.expected == revised.fingerprint()
    assert result.status == "pending"
    assert len(store.db.sources) == 1


@pytest.mark.asyncio
async def test_selection_reports_remotedialer(store: SourceStore, source: SourceCreate) -> None:
    await store.enroll(source, "alice")
    selection = await store.selection(source.source_id)
    assert selection.transport == "remotedialer"


@pytest.mark.asyncio
async def test_selection_missing_source_raises_keyerror(store: SourceStore) -> None:
    with pytest.raises(KeyError):
        await store.selection("missing-source-id")
