"""Regression tests for LiteLLMClient per-op error resilience.

The per-op helpers (_delete_one, _register_one, _patch_one) must gracefully
degrade on ANY error (not just httpx.HTTPStatusError). Because batch() fans out
with asyncio.gather (no return_exceptions=True), a bare ConnectError/Timeout
in one model op would otherwise propagate and abort the entire reconcile,
dropping the remaining deletes/registers/patches.
"""

from unittest.mock import AsyncMock

import httpx
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


class _RaisingClient:
    """An httpx.AsyncClient stand-in that raises a transient network error."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def post(self, *args, **kwargs):
        raise self._exc

    async def patch(self, *args, **kwargs):
        raise self._exc


@pytest.mark.asyncio
async def test_delete_one_degrades_on_connect_error():
    client = LiteLLMClient(read_only=False)
    result = await client._delete_one(_RaisingClient(httpx.ConnectError("boom")), "mid-1")
    assert result is False


@pytest.mark.asyncio
async def test_register_one_degrades_on_timeout():
    client = LiteLLMClient(read_only=False)
    result = await client._register_one(
        _RaisingClient(httpx.ReadTimeout("timed out")), _make_model()
    )
    assert result is None


@pytest.mark.asyncio
async def test_patch_one_degrades_on_connect_error():
    client = LiteLLMClient(read_only=False)
    result = await client._patch_one(
        _RaisingClient(httpx.ConnectError("boom")), "litellm-id", {"model": "m"}
    )
    assert result is False


@pytest.mark.asyncio
async def test_register_payload_stamps_api_surface_for_hamsa_v1():
    """A hamsa pod sniffed as the v1 surface must register litellm_params with
    api_surface=v1 so the gateway builds /v1/speech URLs."""
    captured = {}

    class _CaptureClient:
        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return httpx.Response(200, json={"model_id": "mid-1"}, request=httpx.Request("POST", url))

    client = LiteLLMClient(read_only=False)
    model = _make_model(model_id="hamsa-tts-new", provider="hamsa")
    model.api_surface = "v1"

    result = await client._register_one(_CaptureClient(), model)
    assert result == "mid-1"
    assert captured["json"]["litellm_params"]["api_surface"] == "v1"
    # And the hamsa api_base stays bare (no /v1) regardless of surface.
    assert captured["json"]["litellm_params"]["api_base"].endswith(":8080")


@pytest.mark.asyncio
async def test_register_payload_omits_api_surface_when_unset():
    captured = {}

    class _CaptureClient:
        async def post(self, url, headers=None, json=None):
            captured["json"] = json
            return httpx.Response(200, json={"model_id": "mid-2"}, request=httpx.Request("POST", url))

    client = LiteLLMClient(read_only=False)
    await client._register_one(_CaptureClient(), _make_model(model_id="llama-3"))
    assert "api_surface" not in captured["json"]["litellm_params"]


@pytest.mark.asyncio
async def test_batch_preserves_none_placeholders_for_failed_registers():
    """batch() must keep None for a failed register so callers can align ids to
    inputs by position.

    Regression: batch() used to do `registered_ids = [r for r in reg_results if r]`,
    dropping the failed register's None. execute() then zipped the shorter list
    against ALL registers, shifting ids onto the wrong models after a failure.
    """
    client = LiteLLMClient(read_only=False)

    async def _reg_success(*args, **kwargs):
        return "id-1"

    async def _reg_fail(*args, **kwargs):
        return None  # e.g. _register_one caught a network error

    async def _reg_success2(*args, **kwargs):
        return "id-3"

    client._delete_one = AsyncMock(return_value=True)
    client._patch_one = AsyncMock(return_value=True)

    # Override _register_one with per-call behavior via a side-effect list.
    calls = [_reg_success, _reg_fail, _reg_success2]
    call_iter = iter(calls)

    async def _register_one(*args, **kwargs):
        return await next(call_iter)()

    client._register_one = _register_one

    _, registered, _ = await client.batch(
        deletes=["del-1"],
        registers=[(_make_model("m-a"), None), (_make_model("m-b"), None), (_make_model("m-c"), None)],
        patches=[],
    )

    # Position-preserving: the failed register (None) stays at index 1.
    assert registered == ["id-1", None, "id-3"]