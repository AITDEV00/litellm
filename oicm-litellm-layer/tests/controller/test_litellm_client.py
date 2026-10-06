"""Regression tests for LiteLLMClient per-op error resilience.

The per-op helpers (_delete_one, _register_one, _patch_one) must gracefully
degrade on ANY error (not just httpx.HTTPStatusError). Because batch() fans out
with asyncio.gather (no return_exceptions=True), a bare ConnectError/Timeout
in one model op would otherwise propagate and abort the entire reconcile,
dropping the remaining deletes/registers/patches.
"""

from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from controller.litellm_client import LiteLLMClient, _register_payload
from controller.models import OicmModel


def _make_model(model_id="test-model", provider="hosted_vllm", mode="chat", api_surface=None):
    return OicmModel(
        uuid="uuid-1",
        model_id=model_id,
        model_name=model_id,
        namespace="adeo",
        ready_replicas=1,
        total_replicas=1,
        mode=mode,
        provider=provider,
        api_surface=api_surface,
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
    client = LiteLLMClient(read_only=False, client=_RaisingClient(httpx.ConnectError("boom")))
    result = await client._delete_one("mid-1")
    assert result is False


@pytest.mark.asyncio
async def test_register_one_degrades_on_timeout():
    client = LiteLLMClient(read_only=False, client=_RaisingClient(httpx.ReadTimeout("timed out")))
    result = await client._register_one(_make_model())
    assert result is None


@pytest.mark.asyncio
async def test_patch_one_degrades_on_connect_error():
    client = LiteLLMClient(read_only=False, client=_RaisingClient(httpx.ConnectError("boom")))
    result = await client._patch_one("litellm-id", {"model": "m"})
    assert result is False


@pytest.mark.asyncio
async def test_register_payload_stamps_api_surface_for_hamsa_v1():
    """A hamsa pod sniffed as the v1 surface must register litellm_params with
    api_surface=v1 so the gateway builds /v1/speech URLs."""
    captured = {}

    class _CaptureClient:
        async def post(self, url, **kwargs):
            captured["json"] = kwargs["json"]
            return httpx.Response(200, json={"model_id": "mid-1"}, request=httpx.Request("POST", url))

    client = LiteLLMClient(read_only=False, client=_CaptureClient())
    model = _make_model(model_id="hamsa-tts-new", provider="hamsa", api_surface="v1")

    result = await client._register_one(model)
    assert result == "mid-1"
    assert captured["json"]["litellm_params"]["api_surface"] == "v1"
    # And the hamsa api_base stays bare (no /v1) regardless of surface.
    assert captured["json"]["litellm_params"]["api_base"].endswith(":8080")


@pytest.mark.asyncio
async def test_register_payload_omits_api_surface_when_unset():
    captured = {}

    class _CaptureClient:
        async def post(self, url, **kwargs):
            captured["json"] = kwargs["json"]
            return httpx.Response(200, json={"model_id": "mid-2"}, request=httpx.Request("POST", url))

    client = LiteLLMClient(read_only=False, client=_CaptureClient())
    await client._register_one(_make_model(model_id="llama-3"))
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


@pytest.mark.asyncio
async def test_list_all_models_groups_by_uuid_alone():
    """The gateway join key must be the deployment uuid, not `{uuid}::{name}`.

    OICM's `model_name` is the deployment's GUI label, which can differ from the
    id the server serves. The gateway row is matched by uuid, so grouping on a
    composite that includes the name would break the join for every renamed
    deployment.
    """
    payload = {
        "data": [
            {
                "model_id": "gw-1",
                "model_name": "zai-org/GLM-5.3",
                "model_info": {"id": "gw-1", "oicm_uuid": "9dcd9568"},
            },
            {
                # Same uuid, but the gateway name differs from OICM's label.
                "model_name": "Qwen/Qwen3.6-35B-A3B-FP8",
                "model_info": {"id": "gw-2", "oicm_uuid": "894cea22"},
            },
            {
                # No oicm_uuid: not controller-managed, so it is not grouped.
                "model_name": "admin-added",
                "model_info": {"id": "gw-3"},
            },
        ]
    }

    class _InfoClient:
        async def get(self, url, **kwargs):
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    client = LiteLLMClient(read_only=False, client=_InfoClient())
    grouped = await client.list_all_models_by_key()

    assert set(grouped.keys()) == {"9dcd9568", "894cea22"}
    assert grouped["9dcd9568"][0]["model_id"] == "gw-1"
    assert grouped["894cea22"][0]["model_id"] == "gw-2"

@pytest.mark.asyncio
async def test_list_all_models_strips_the_submariner_prefix_when_grouping():
    """A cross-cluster row must group under the uuid its own OICM reports.

    A Submariner import stores `submariner:abudhabi:<uuid>` while Abu Dhabi's
    OICM returns the bare uuid. Grouping on the namespaced value would mean the
    row could never find its status, because the poller keys snapshots by the
    bare uuid.
    """
    payload = {
        "data": [
            {
                "model_name": "zai-org/GLM-5.2-FP8",
                "model_info": {
                    "id": "gw-ad",
                    "oicm_uuid": "submariner:abudhabi:766b1720",
                },
            },
            {
                "model_name": "Qwen/Qwen3.6-35B-A3B-FP8",
                "model_info": {"id": "gw-local", "oicm_uuid": "894cea22"},
            },
        ]
    }

    class _InfoClient:
        async def get(self, url, **kwargs):
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))

    client = LiteLLMClient(read_only=False, client=_InfoClient())
    grouped = await client.list_all_models_by_key()

    assert set(grouped.keys()) == {"766b1720", "894cea22"}
    assert grouped["766b1720"][0]["model_id"] == "gw-ad"


@pytest.mark.asyncio
async def test_patch_status_sends_blocked_cluster_and_the_block_in_one_body():
    """A status change must be one write carrying both owners' keys.

    `blocked` is a top-level column and the rest is a nested `model_info`
    object; sending them separately would be two reloads for one fact change.
    The cluster and source name ride along so a row registered before either
    field existed gains them on its first status write.
    """
    seen = {}

    class _PatchClient:
        async def patch(self, url, json=None, **kwargs):
            seen["url"] = url
            seen["json"] = json
            return httpx.Response(200, json={"message": "ok"}, request=httpx.Request("PATCH", url))

    client = LiteLLMClient(read_only=False, client=_PatchClient())
    ok = await client.patch_status(
        "mid-1", True, "abudhabi", "abudhabi-oicm", {"v": 1, "status": "Stopped"}
    )

    assert ok is True
    assert seen["url"].endswith("/model/mid-1/update")
    assert seen["json"]["blocked"] is True
    assert seen["json"]["model_info"]["oicm_cluster"] == "abudhabi"
    assert seen["json"]["model_info"]["oicm_source_name"] == "abudhabi-oicm"
    assert seen["json"]["model_info"]["oicm"] == {"v": 1, "status": "Stopped"}


@pytest.mark.asyncio
async def test_register_payload_stamps_the_cluster():
    """A registered row must say which cluster its deployment is in.

    The uuid alone cannot answer that once a cross-cluster import shares a model
    name with a local deployment, so the cluster is stored alongside it.
    """
    model = _make_model()
    model = replace(model, cluster="abudhabi")

    payload = _register_payload(model, None)

    assert payload["model_info"]["oicm_cluster"] == "abudhabi"
