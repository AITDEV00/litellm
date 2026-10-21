"""Regression tests for the audio pod URL builder.

The custom voice routes forward to an audio deployment's ``api_base``, which is
registered as ``http://host:8080/v1`` while the forwarded paths also start with
``/v1``. Naive concatenation produced ``/v1/v1/models``, which the pod answers
with 404, so ``GET /v1/audio/models`` and ``GET /v1/audio/health`` were broken
even though the routes were registered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Final

import pytest

from litellm.proxy.voice_routes import (
    _audio_speech_chunk_generator,
    _join_audio_url,
    _resolve_audio_api_base,
    _resolve_audio_model,
    _resolve_audio_target_url,
    _resolve_voice_model,
)


class _FakeRouter:
    def __init__(self, model_list: list[dict]) -> None:
        self.model_list = model_list


@pytest.mark.parametrize(
    ("api_base", "path", "expected"),
    [
        ("http://host:8080/v1", "/v1/models", "http://host:8080/v1/models"),
        ("http://host:8080/v1/", "/v1/models", "http://host:8080/v1/models"),
        ("http://host:8080", "/v1/models", "http://host:8080/v1/models"),
        ("http://host:8080/v1", "/v1/models/abc", "http://host:8080/v1/models/abc"),
        ("http://host:8080/v1", "/health", "http://host:8080/health"),
        ("http://host:8080", "/health", "http://host:8080/health"),
        ("http://host:8080/v1", "/metrics", "http://host:8080/metrics"),
    ],
)
def test_pod_path_is_anchored_at_the_host_root(api_base: str, path: str, expected: str) -> None:
    assert _join_audio_url(api_base, path) == expected


def test_target_url_uses_the_registered_deployment_api_base() -> None:
    router = _FakeRouter(
        [{"litellm_params": {"model": "omnivoice/omnivoice", "api_base": "http://pod:8080/v1"}}]
    )

    assert _resolve_audio_target_url(router, "omnivoice", "/v1/models") == (
        "http://pod:8080/v1/models"
    )


def test_target_url_is_none_when_the_provider_is_not_registered() -> None:
    router = _FakeRouter(
        [{"litellm_params": {"model": "hamsa/hamsa-tts", "api_base": "http://pod:8080/v1"}}]
    )

    assert _resolve_audio_target_url(router, "omnivoice", "/v1/models") is None


class _FakeBinaryResponse:
    """Minimal stand-in for ``HttpxBinaryResponseContent``.

    ``aiter_bytes`` is a coroutine that returns an async iterator, matching the
    openai SDK. A caller that forgets to await it gets a coroutine, and
    ``async for`` over that raises TypeError.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def aiter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            for chunk in self.chunks:
                yield chunk

        return _gen()

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_chunk_generator_relays_audio_bytes() -> None:
    response = _FakeBinaryResponse([b"aaa", b"bbb"])

    relayed = [chunk async for chunk in _audio_speech_chunk_generator(response)]

    assert relayed == [b"aaa", b"bbb"]
    assert response.closed is True


def test_audio_model_resolution_prefers_a_speech_deployment() -> None:
    router = _FakeRouter(
        [
            {"model_name": "hamsa-stt", "litellm_params": {"model": "hamsa/hamsa-stt", "mode": "audio_transcription"}},
            {"model_name": "omnivoice", "litellm_params": {"model": "omnivoice/omnivoice", "mode": "audio_speech"}},
        ]
    )

    assert _resolve_audio_model(router) == "omnivoice"


def test_audio_model_resolution_ignores_deployments_without_the_speech_mode() -> None:
    router = _FakeRouter(
        [
            {"model_name": "hamsa-stt", "litellm_params": {"model": "hamsa/hamsa-stt", "mode": "audio_transcription"}},
        ]
    )

    assert _resolve_audio_model(router) is None


def test_audio_model_resolution_reads_mode_from_model_info() -> None:
    """Mode lives in litellm_params for config-file deployments and in
    model_info for API-created ones; both must be recognised."""
    router = _FakeRouter(
        [
            {
                "model_name": "hamsa-tts",
                "litellm_params": {"model": "hamsa/hamsa-tts", "mode": "audio_speech"},
            },
            {
                "model_name": "omnivoice",
                "litellm_params": {"model": "omnivoice/omnivoice"},
                "model_info": {"mode": "audio_speech"},
            },
        ]
    )

    assert _resolve_audio_model(router) == "hamsa-tts"


def test_audio_model_resolution_finds_api_created_deployments_alone() -> None:
    router = _FakeRouter(
        [
            {"model_name": "hamsa-stt", "litellm_params": {"model": "hamsa/hamsa-stt"}},
            {
                "model_name": "omnivoice",
                "litellm_params": {"model": "omnivoice/omnivoice"},
                "model_info": {"mode": "audio_speech"},
            },
        ]
    )

    assert _resolve_audio_model(router) == "omnivoice"


def test_provider_qualified_match_outranks_mode_match() -> None:
    """An explicitly requested provider must never be shadowed by a deployment
    that merely declares audio_speech and happens to be listed first."""
    router = _FakeRouter(
        [
            {
                "model_name": "inception-tts",
                "litellm_params": {"model": "inception/inception-tts", "api_base": "http://inc:8080/v1"},
                "model_info": {"mode": "audio_speech"},
            },
            {
                "model_name": "omnivoice",
                "litellm_params": {"model": "omnivoice/omnivoice", "api_base": "http://omni:8080/v1"},
                "model_info": {"mode": "audio_speech"},
            },
        ]
    )

    assert _resolve_audio_model(router, "omnivoice") == "omnivoice"
    assert _resolve_audio_api_base(router, "omnivoice") == "http://omni:8080/v1"


def test_api_base_falls_through_to_the_next_deployment_when_unset() -> None:
    """A mode match with no api_base must not end the search."""
    router = _FakeRouter(
        [
            {"model_name": "no-base", "model_info": {"mode": "audio_speech"}},
            {
                "model_name": "has-base",
                "litellm_params": {"model": "omnivoice/omnivoice", "api_base": "http://pod:8080/v1"},
                "model_info": {"mode": "audio_speech"},
            },
        ]
    )

    assert _resolve_audio_api_base(router) == "http://pod:8080/v1"


def test_provider_scoped_lookup_does_not_fall_through_to_another_provider() -> None:
    """An explicitly requested provider must never resolve to a different one.

    The health, models and metrics routes forward to a named provider's pod. If
    no deployment of that provider exists, returning another provider's api_base
    forwards the request to a server that cannot answer it, which is worse than
    reporting that the provider is unregistered.
    """
    router = _FakeRouter(
        [
            {
                "model_name": "hamsa-tts",
                "litellm_params": {
                    "model": "hamsa/hamsa-tts",
                    "mode": "audio_speech",
                    "api_base": "http://hamsa:8080/v1",
                },
            },
            {
                "model_name": "omnivoice",
                "litellm_params": {
                    "model": "omnivoice/omnivoice",
                    "mode": "audio_speech",
                    "api_base": "http://omnivoice:8080/v1",
                },
            },
        ]
    )

    assert _resolve_audio_api_base(router, provider="omnivoice") == "http://omnivoice:8080/v1"
    assert _resolve_audio_api_base(router, provider="inception") is None


def test_voice_management_fallback_skips_providers_without_voice_support() -> None:
    """A no-model voice-management request must resolve to a provider that
    supports voice actions, not whichever audio deployment registered first.

    The gateway registers inception-tts before the voice-capable providers, so
    the generic audio fallback sent every no-model /v1/voices request to a
    provider that rejects voice management outright.
    """
    router: Final = _FakeRouter(
        [
            {
                "model_name": "inception-tts",
                "litellm_params": {"model": "inception/inception-tts", "mode": "audio_speech"},
            },
            {
                "model_name": "hamsa-tts-new",
                "litellm_params": {"model": "hamsa/hamsa-tts-new", "mode": "audio_speech"},
            },
        ]
    )

    assert _resolve_audio_model(router) == "inception-tts"
    assert _resolve_voice_model(router, action="register") == "hamsa-tts-new"


def test_voice_list_skips_providers_without_a_read_api() -> None:
    """A read action must only resolve to a provider that serves it.

    Hamsa implements voice registration and loading but has no list
    endpoint, so a no-model list request routed there failed Hamsa's
    register validation. The read actions must skip it and land on the
    one deployment that serves them.
    """
    router: Final = _FakeRouter(
        [
            {
                "model_name": "hamsa-tts-new",
                "litellm_params": {"model": "hamsa/hamsa-tts-new", "mode": "audio_speech"},
            },
            {
                "model_name": "omnivoice",
                "litellm_params": {"model": "omnivoice/omnivoice", "mode": "audio_speech"},
            },
        ]
    )

    assert _resolve_voice_model(router, action="register") == "hamsa-tts-new"
    assert _resolve_voice_model(router, action="list") == "omnivoice"
    assert _resolve_voice_model(router, action="list_profiles") == "omnivoice"
    assert _resolve_voice_model(router, action="get_profile") == "omnivoice"


def test_voice_management_falls_back_to_none_when_no_provider_supports_voice() -> None:
    router: Final = _FakeRouter(
        [
            {
                "model_name": "inception-tts",
                "litellm_params": {"model": "inception/inception-tts", "mode": "audio_speech"},
            },
        ]
    )

    assert _resolve_voice_model(router, action="register") is None
