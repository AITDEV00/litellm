"""Tests for LiteLLMClient read-only mode (debug controller safety)."""

from unittest.mock import AsyncMock

import pytest

from controller.litellm_client import LiteLLMClient
from controller.models import OicmModel


def _make_model(model_id="test-model", provider="hosted_vllm", mode="chat"):
    return OicmModel(
        uuid="uuid-1",
        model_id=model_id,
        model_name=model_id,
        namespace="adeo",
        ready_replicas=1,
        total_replicas=1,
        mode=mode,
        provider=provider,
    )


@pytest.mark.asyncio
async def test_read_only_batch_does_not_write(caplog):
    client = LiteLLMClient(read_only=True)
    # If read_only were ignored, batch() would try to open an httpx client and
    # POST to the real gateway. Ensure the write helpers are never called.
    client._delete_one = AsyncMock(return_value=True)
    client._register_one = AsyncMock(return_value="fake-id")
    client._patch_one = AsyncMock(return_value=True)

    deleted, registered, patched = await client.batch(
        deletes=["litellm-id-del"],
        registers=[(_make_model(), None)],
        patches=[("litellm-id-p", {"model": "m"}, None)],
    )

    assert deleted == 0
    assert registered == []
    assert patched == 0
    client._delete_one.assert_not_awaited()
    client._register_one.assert_not_awaited()
    client._patch_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_only_register_model_is_noop(caplog):
    client = LiteLLMClient(read_only=True)
    client._register_one = AsyncMock(return_value="fake-id")

    result = await client.register_model(_make_model())

    assert result is None
    client._register_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_only_deregister_is_noop(caplog):
    client = LiteLLMClient(read_only=True)
    client._delete_one = AsyncMock(return_value=True)

    result = await client.deregister_model("litellm-id")

    assert result is False
    client._delete_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_write_batch_still_writes(monkeypatch):
    client = LiteLLMClient(read_only=False)
    # Simulate successful writes to confirm the normal (non-debug) path still
    # issues the write calls.
    client._delete_one = AsyncMock(return_value=True)
    client._register_one = AsyncMock(return_value="new-id")
    client._patch_one = AsyncMock(return_value=True)

    deleted, registered, patched = await client.batch(
        deletes=["litell-1"],
        registers=[(_make_model(), None)],
        patches=[("litell-2", {"model": "m"}, None)],
    )

    assert deleted == 1
    assert registered == ["new-id"]
    assert patched == 1
    client._delete_one.assert_awaited_once()
    client._register_one.assert_awaited_once()
    client._patch_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_only_logs_would_write(caplog):
    client = LiteLLMClient(read_only=True)
    with caplog.at_level("INFO"):
        await client.register_model(_make_model())
    assert any("[READ-ONLY]" in r.message for r in caplog.records)