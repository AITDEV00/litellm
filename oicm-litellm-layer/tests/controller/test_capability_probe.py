"""Tests for the capability probing in LocalDeploymentSource.

The probe chain has real cost semantics: the sglang /model_info read is free
metadata, the synthetic image/audio requests are real inferences sent
straight to the pod. The tests pin the precedence (metadata beats synthetic),
the memoization (synthetic runs once per deployment per process), and the
failure degradations (unknown claims nothing, never a wrong claim).
"""

import httpx
import pytest

from controller.capabilities import CapabilityFacts
from controller.sources.local_deployments import LocalDeploymentSource


def _source():
    source = LocalDeploymentSource.__new__(LocalDeploymentSource)
    source._synthetic_probe_cache = {}
    return source


class _Client:
    """Stands in for httpx.AsyncClient with canned per-URL responses."""

    def __init__(self, responses):
        self._responses = responses
        self.requests = []

    async def get(self, url):
        self.requests.append(("GET", url))
        status, body = self._responses.get(url, (404, {}))
        return httpx.Response(
            status, json=body, request=httpx.Request("GET", url)
        )

    async def post(self, url, **_kwargs):
        self.requests.append(("POST", url))
        status, body = self._responses.get(url, (500, {}))
        return httpx.Response(
            status, json=body, request=httpx.Request("POST", url)
        )


_SGLANG_INFO = (
    "http://s-uuid-1.adeo.svc.cluster.local:8080/model_info",
    (
        200,
        {"has_image_understanding": True, "has_audio_understanding": False},
    ),
)


@pytest.mark.asyncio
async def test_sglang_metadata_wins_and_costs_no_inference():
    """A server that self-reports its modalities must never be asked to run
    one; the metadata answer is authoritative."""
    client = _Client({_SGLANG_INFO[0]: _SGLANG_INFO[1]})
    source = _source()
    source._client = client

    facts = await source._probe_capability_facts("uuid-1", "chat", "m", frozenset())

    assert facts == CapabilityFacts(accepts_image=True, accepts_audio=False)
    assert all(kind == "GET" for kind, _ in client.requests)
    assert ("POST", "http://s-uuid-1.adeo.svc.cluster.local:8080/v1/chat/completions") not in client.requests


@pytest.mark.asyncio
async def test_ti2i_server_gets_no_stamp_even_with_image_flag():
    """The TI2I server reports has_image_understanding=True for its edit path;
    its own served task types suppress the stamp so understanding-style image
    chats are never routed to a diffusion server."""
    client = _Client({_SGLANG_INFO[0]: _SGLANG_INFO[1]})
    source = _source()
    source._client = client

    facts = await source._probe_capability_facts(
        "uuid-1", "chat", "m", frozenset({"TI2I"})
    )

    assert facts == CapabilityFacts()


@pytest.mark.asyncio
async def test_no_metadata_falls_back_to_synthetic_probes():
    """A server with no /model_info surface (vLLM) is asked directly: one
    image request and one audio request, both to the pod."""
    client = _Client(
        {
            "http://s-uuid-2.adeo.svc.cluster.local:8080/v1/chat/completions": (
                200,
                {"choices": [{"message": {"content": "red"}}]},
            ),
        }
    )
    source = _source()
    source._client = client

    facts = await source._probe_capability_facts("uuid-2", "chat", "m", frozenset())

    assert facts == CapabilityFacts(accepts_image=True, accepts_audio=True)
    posts = [url for kind, url in client.requests if kind == "POST"]
    assert len(posts) == 2
    assert source._synthetic_probe_cache["uuid-2"] == (True, True)


@pytest.mark.asyncio
async def test_400_rejection_is_a_definitive_negative():
    client = _Client(
        {
            "http://s-uuid-3.adeo.svc.cluster.local:8080/v1/chat/completions": (
                400,
                {"error": "Model only supports text input"},
            ),
        }
    )
    source = _source()
    source._client = client

    facts = await source._probe_capability_facts("uuid-3", "chat", "m", frozenset())

    assert facts == CapabilityFacts(accepts_image=False, accepts_audio=False)


@pytest.mark.asyncio
async def test_synthetic_verdict_is_memoized_per_process():
    """The synthetic probe is the only expensive step in discovery; a second
    cycle must read the cache, not re-run the inference."""
    client = _Client(
        {
            "http://s-uuid-4.adeo.svc.cluster.local:8080/v1/chat/completions": (
                200,
                {},
            ),
        }
    )
    source = _source()
    source._client = client

    await source._probe_capability_facts("uuid-4", "chat", "m", frozenset())
    post_count_first = sum(1 for kind, _ in client.requests if kind == "POST")
    await source._probe_capability_facts("uuid-4", "chat", "m", frozenset())

    posts = sum(1 for kind, _ in client.requests if kind == "POST")
    assert post_count_first == 2
    assert posts == 2, "second cycle must reuse the memoized verdicts"


@pytest.mark.asyncio
async def test_inconclusive_probe_retries_next_cycle():
    """A 5xx says nothing about the capability: no claim is stamped AND the
    cache stays empty so the next cycle probes again."""
    client = _Client(
        {
            "http://s-uuid-5.adeo.svc.cluster.local:8080/v1/chat/completions": (
                503,
                {},
            ),
        }
    )
    source = _source()
    source._client = client

    facts = await source._probe_capability_facts("uuid-5", "chat", "m", frozenset())

    assert facts == CapabilityFacts(accepts_image=None, accepts_audio=None)
    assert "uuid-5" not in source._synthetic_probe_cache


@pytest.mark.asyncio
async def test_non_chat_mode_probes_nothing():
    """A TTS or transcription deployment never runs the capability chain:
    its mode is not a chat surface and the stamps would be noise."""
    client = _Client({})
    source = _source()
    source._client = client

    facts = await source._probe_capability_facts("uuid-6", "text_to_speech", "m", frozenset())

    assert facts is None
    assert client.requests == []


@pytest.mark.asyncio
async def test_metadata_200_without_flags_falls_through_to_synthetic():
    """A 200 that carries no modality flags is a surface with nothing to say;
    the synthetic probe still applies."""
    client = _Client(
        {
            _SGLANG_INFO[0]: (200, {"is_generation": True}),
            "http://s-uuid-7.adeo.svc.cluster.local:8080/v1/chat/completions": (
                200,
                {},
            ),
        }
    )
    source = _source()
    source._client = client

    facts = await source._probe_capability_facts("uuid-7", "chat", "m", frozenset())

    assert facts == CapabilityFacts(accepts_image=True, accepts_audio=True)


@pytest.mark.asyncio
async def test_probe_error_on_metadata_falls_through_to_synthetic():
    """A connection error on /model_info is a probe failure, not a negative;
    the synthetic probe still runs."""
    class _ErrClient:
        async def get(self, _url):
            raise httpx.ConnectError("refused")

        async def post(self, _url, **_kwargs):
            return httpx.Response(200, json={}, request=httpx.Request("POST", _url))

    source = _source()
    source._client = _ErrClient()

    facts = await source._probe_capability_facts("uuid-8", "chat", "m", frozenset())

    assert facts == CapabilityFacts(accepts_image=True, accepts_audio=True)
