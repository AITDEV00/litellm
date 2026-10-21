"""Regression tests for the audio pod URL builder.

The custom voice routes forward to an audio deployment's ``api_base``, which is
registered as ``http://host:8080/v1`` while the forwarded paths also start with
``/v1``. Naive concatenation produced ``/v1/v1/models``, which the pod answers
with 404, so ``GET /v1/audio/models`` and ``GET /v1/audio/health`` were broken
even though the routes were registered.
"""

from __future__ import annotations

import pytest

from litellm.proxy.voice_routes import _join_audio_url, _resolve_audio_target_url


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
def test_pod_path_is_anchored_at_the_host_root(api_base: str, path: str, expected: str):
    assert _join_audio_url(api_base, path) == expected


def test_target_url_uses_the_registered_deployment_api_base():
    router = _FakeRouter(
        [{"litellm_params": {"model": "omnivoice/omnivoice", "api_base": "http://pod:8080/v1"}}]
    )

    assert _resolve_audio_target_url(router, "omnivoice", "/v1/models") == (
        "http://pod:8080/v1/models"
    )


def test_target_url_is_none_when_the_provider_is_not_registered():
    router = _FakeRouter(
        [{"litellm_params": {"model": "hamsa/hamsa-tts", "api_base": "http://pod:8080/v1"}}]
    )

    assert _resolve_audio_target_url(router, "omnivoice", "/v1/models") is None
