"""Regression tests for the audio pod URL builder.

The custom voice routes forward to an audio deployment's ``api_base``, which is
registered as ``http://host:8080/v1`` while the forwarded paths also start with
``/v1``. Naive concatenation produced ``/v1/v1/models``, which the pod answers
with 404, so ``GET /v1/audio/models`` and ``GET /v1/audio/health`` were broken
even though the routes were registered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse

from litellm.proxy import proxy_server, voice_routes
from litellm.proxy.common_utils.http_parsing_utils import read_request_body
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


_HIDDEN: Final = {
    "_hidden_params": {
        "model_id": "9cf166e2-02b5-497e-8ec6-4dcd48a58b4e",
        "litellm_call_id": "call-abc",
        "additional_headers": {"x-litellm-model-group": "fishaudio/drama-3"},
    }
}


def _mnemonic_request(path: str, payload: bytes = b"", query_string: bytes = b"") -> Request:
    """A request with the shape _route_voice_management actually reads.

    The route resolves model from body or form, then from the query string, so a
    regression test that mirrors only the body cannot catch a scope bug. This
    helper exposes the query string the same way Starlette does.
    """
    request = Request(
        {
            "type": "http",
            "method": "POST" if payload else "GET",
            "path": path,
            "headers": Headers({"content-type": "application/json"}).raw,
            "query_string": query_string,
        }
    )

    async def _receive() -> dict:
        return {"type": "http.request", "body": payload, "more_body": False}

    request._receive = _receive  # pyright: ignore[reportPrivateUsage]  # starlette offers no public body-injection hook
    return request


@pytest.mark.asyncio
async def test_create_voice_consumed_multipart_reaches_missing_audio_validation():
    encoded: Final = httpx.Request(
        "POST",
        "http://test/v1/audio/voices",
        data={"name": "GuideTest", "consent": "Test only", "ref_text": "Sample"},
        files={"ref_audio": ("empty.wav", b"", "application/octet-stream")},
    )
    body: Final = encoded.read()
    messages: Final = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    request: Final = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/audio/voices",
            "headers": [(b"content-type", encoded.headers["content-type"].encode())],
            "query_string": b"model=fishaudio/drama-3",
        },
        receive=receive,
    )
    form = await request.form()
    parsed = await read_request_body(request)
    assert parsed["name"] == "GuideTest"
    assert parsed["consent"] == "Test only"
    assert parsed["ref_text"] == "Sample"
    assert await form["ref_audio"].read() == b""
    assert parsed == await read_request_body(request)


class TestVoiceManagementModelScoping:
    """A no-model voice call must be scopable with ?model=, not silently fall
    through to the first voice-capable deployment.

    Without the fix the resolver ignores the query string, so a GET /v1/voices
    returns whatever provider _resolve_voice_model picks first (OmniVoice on a
    multi-provider gateway), and the caller's intent is lost.
    """

    def _patch_router(self, monkeypatch: pytest.MonkeyPatch, models: list[dict]) -> Any:
        # keep a live deployment list; the scoping assertions only mean anything
        # if _resolve_voice_model could have picked the OTHER deployment
        router: Final = _FakeRouter(models)
        return router

    def _route_ready(
        self, monkeypatch: pytest.MonkeyPatch, router: Any, resolved: dict[str, Any]
    ) -> None:
        async def _route(**kwargs: Any) -> Any:
            resolved["model"] = kwargs["data"]["model"]

            async def _done() -> dict:
                return {"voices": []}

            return _done()

        async def _add_data(**kwargs: Any) -> Any:
            return kwargs["data"]

        monkeypatch.setattr(voice_routes, "route_request", _route)
        monkeypatch.setattr(voice_routes, "add_litellm_data_to_request", _add_data)
        monkeypatch.setattr("litellm.proxy.proxy_server.llm_router", router, raising=False)
        monkeypatch.setattr("litellm.proxy.proxy_server.user_model", None, raising=False)

        logging_obj = MagicMock()
        logging_obj.pre_call_hook = AsyncMock(side_effect=lambda **kw: kw["data"])
        logging_obj.update_request_status = AsyncMock()
        logging_obj.post_call_failure_hook = AsyncMock()
        logging_obj.post_call_response_headers_hook = AsyncMock(return_value={})
        monkeypatch.setattr("litellm.proxy.proxy_server.proxy_logging_obj", logging_obj)
        monkeypatch.setattr("litellm.proxy.proxy_server.general_settings", {}, raising=False)
        monkeypatch.setattr("litellm.proxy.proxy_server.version", "0.0.0-test", raising=False)
        monkeypatch.setattr("litellm.proxy.proxy_server.proxy_config", None, raising=False)

    @pytest.mark.asyncio
    async def test_list_voices_honors_the_query_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models: Final = [
            {"model_name": "omnivoice", "litellm_params": {"model": "omnivoice/omnivoice", "mode": "audio_speech"}},
            {"model_name": "drama3", "litellm_params": {"model": "drama3/drama-3", "mode": "audio_speech"}},
        ]
        router: Final = self._patch_router(monkeypatch, models)
        resolved: dict[str, Any] = {}
        self._route_ready(monkeypatch, router, resolved)

        # omnivoice is registered first, so a query-less request would pick it.
        # With ?model=drama3 the caller's explicit scope must win.
        request = _mnemonic_request("/v1/voices", query_string=b"model=drama3")
        await voice_routes.list_voices(
            request=request,
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region="", models=None, team_models=None),
        )

        assert resolved.get("model") == "drama3"

    @pytest.mark.asyncio
    async def test_create_voice_honors_the_query_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models: Final = [
            {"model_name": "omnivoice", "litellm_params": {"model": "omnivoice/omnivoice", "mode": "audio_speech"}},
            {"model_name": "drama3", "litellm_params": {"model": "drama3/drama-3", "mode": "audio_speech"}},
        ]
        router: Final = self._patch_router(monkeypatch, models)
        resolved: dict[str, Any] = {}
        self._route_ready(monkeypatch, router, resolved)

        request = _mnemonic_request("/v1/audio/voices", query_string=b"model=drama3")
        await voice_routes.create_voice(
            request=request,
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region="", models=None, team_models=None),
        )

        assert resolved.get("model") == "drama3"

    @pytest.mark.asyncio
    async def test_list_voices_without_a_scope_still_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # the convenience path must keep working or we break unscoped callers
        _patched_route(monkeypatch, {"voices": []})
        resolved: dict[str, Any] = {}

        async def _route(**kwargs: Any) -> Any:
            resolved["model"] = kwargs["data"]["model"]

            async def _done() -> dict:
                return {"voices": []}

            return _done()

        monkeypatch.setattr(voice_routes, "route_request", _route)
        monkeypatch.setattr("litellm.proxy.proxy_server.llm_router", None, raising=False)

        request = _mnemonic_request("/v1/voices")
        await voice_routes.list_voices(
            request=request,
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region="", models=None, team_models=None),
        )

        # model=py_server.user_model is None -> _resolve_voice_model with no router -> None
        assert resolved.get("model") is None


class TestVoiceResolutionHeaders:
    """Unscoped voice calls must surface that they fell back and which model
    actually served them, in headers, so callers do not have to read the
    response body shape to find out which pod answered."""

    def _patch_full(
        self, monkeypatch: pytest.MonkeyPatch, router: Any, body: dict[str, Any]
    ) -> None:
        """Wire a route that returns the given pod body."""

        async def _route(**kwargs: Any) -> Any:
            async def _done() -> dict:
                return body

            return _done()

        async def _add_data(**kwargs: Any) -> Any:
            return kwargs["data"]

        monkeypatch.setattr(voice_routes, "route_request", _route)
        monkeypatch.setattr(voice_routes, "add_litellm_data_to_request", _add_data)
        monkeypatch.setattr("litellm.proxy.proxy_server.llm_router", router, raising=False)
        monkeypatch.setattr("litellm.proxy.proxy_server.user_model", None, raising=False)

        logging_obj = MagicMock()
        logging_obj.pre_call_hook = AsyncMock(side_effect=lambda **kw: kw["data"])
        logging_obj.update_request_status = AsyncMock()
        logging_obj.post_call_failure_hook = AsyncMock()
        logging_obj.post_call_response_headers_hook = AsyncMock(return_value={})
        monkeypatch.setattr("litellm.proxy.proxy_server.proxy_logging_obj", logging_obj)
        monkeypatch.setattr("litellm.proxy.proxy_server.general_settings", {}, raising=False)
        monkeypatch.setattr("litellm.proxy.proxy_server.version", "0.0.0-test", raising=False)
        monkeypatch.setattr("litellm.proxy.proxy_server.proxy_config", None, raising=False)

    @pytest.mark.asyncio
    async def test_unscoped_list_voices_marks_fallback_and_names_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models: Final = [
            {"model_name": "omnivoice", "litellm_params": {"model": "omnivoice/omnivoice", "mode": "audio_speech"}},
        ]
        self._patch_full(monkeypatch, _FakeRouter(models), {"voices": ["default"]})

        result = await voice_routes.list_voices(
            request=_mnemonic_request("/v1/voices"),
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region="", models=None, team_models=None),
        )

        assert isinstance(result, JSONResponse)
        headers: Final = dict(result.headers)
        assert headers.get(voice_routes.VOICE_RESOLUTION_HEADER) == "fallback"
        assert headers.get(voice_routes.VOICE_RESOLVED_MODEL_HEADER) == "omnivoice"

    @pytest.mark.asyncio
    async def test_scoped_list_voices_marks_explicit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models: Final = [
            {"model_name": "drama3", "litellm_params": {"model": "drama3/drama-3", "mode": "audio_speech"}},
        ]
        self._patch_full(monkeypatch, _FakeRouter(models), {"voices": ["default"]})

        result = await voice_routes.list_voices(
            request=_mnemonic_request("/v1/voices", query_string=b"model=drama3"),
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region="", models=None, team_models=None),
        )

        assert isinstance(result, JSONResponse)
        headers: Final = dict(result.headers)
        assert headers.get(voice_routes.VOICE_RESOLUTION_HEADER) == "explicit"
        assert headers.get(voice_routes.VOICE_RESOLVED_MODEL_HEADER) == "drama3"

    @pytest.mark.asyncio
    async def test_unscoped_delete_profile_marks_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models: Final = [
            {"model_name": "omnivoice", "litellm_params": {"model": "omnivoice/omnivoice", "mode": "audio_speech"}},
        ]
        self._patch_full(
            monkeypatch, _FakeRouter(models), {"success": True, "message": "deleted"}
        )

        result = await voice_routes.delete_voice_profile(
            request=_mnemonic_request("/v1/voices/profiles/x"),
            fastapi_response=MagicMock(),
            profile_id="x",
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region="", models=None, team_models=None),
        )

        assert isinstance(result, JSONResponse)
        headers: Final = dict(result.headers)
        assert headers.get(voice_routes.VOICE_RESOLUTION_HEADER) == "fallback"
        assert headers.get(voice_routes.VOICE_RESOLVED_MODEL_HEADER) == "omnivoice"

    @pytest.mark.asyncio
    async def test_no_resolvable_model_omits_both_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # no router + no explicit model -> nothing to advertise
        self._patch_full(monkeypatch, None, {"voices": []})

        result = await voice_routes.list_voices(
            request=_mnemonic_request("/v1/voices"),
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region="", models=None, team_models=None),
        )

        assert isinstance(result, JSONResponse)
        headers: Final = dict(result.headers)
        assert voice_routes.VOICE_RESOLUTION_HEADER not in headers
        assert voice_routes.VOICE_RESOLVED_MODEL_HEADER not in headers


class TestAudioModelsProviderScopeHeaders:
    """The same signal must reach callers of /v1/audio/models et al, which
    proxy raw pod responses, so the header rides on the Response object."""

    def _patch(self, monkeypatch: pytest.MonkeyPatch, models: list[dict]) -> None:
        monkeypatch.setattr(
            "litellm.proxy.proxy_server.llm_router",
            _FakeRouter(models),
            raising=False,
        )
        import httpx as _httpx

        class _Resp:
            def __init__(self) -> None:
                self.content = b"{}"
                self.status_code = 200
                self.headers: dict[str, str] = {"content-type": "application/json"}

        class _Client:
            def __init__(self, **kwargs: Any) -> None:
                pass

            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *_: Any) -> None:
                pass

            async def get(self, *args: Any, **kwargs: Any) -> Any:
                return _Resp()

        monkeypatch.setattr(_httpx, "AsyncClient", _Client)

    @pytest.mark.asyncio
    async def test_unscoped_audio_models_marks_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models: Final = [
            {"model_name": "omnivoice", "litellm_params": {"model": "omnivoice/omnivoice", "mode": "audio_speech", "api_base": "http://omni:8080/v1"}},
        ]
        self._patch(monkeypatch, models)

        request: Final = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/v1/audio/models",
                "headers": Headers({}).raw,
                "query_string": b"",
            }
        )
        result = await voice_routes.audio_models(
            request=request,
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None),
        )

        headers: Final = dict(result.headers)
        assert headers.get(voice_routes.VOICE_RESOLUTION_HEADER) == "fallback"
        assert headers.get(voice_routes.VOICE_RESOLVED_MODEL_HEADER) == "omnivoice"

    @pytest.mark.asyncio
    async def test_scoped_audio_models_marks_explicit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models: Final = [
            {"model_name": "drama3", "litellm_params": {"model": "drama3/drama-3", "mode": "audio_speech", "api_base": "http://drama:8080/v1"}},
        ]
        self._patch(monkeypatch, models)

        request: Final = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/v1/audio/models",
                "headers": Headers({}).raw,
                "query_string": b"provider=drama3",
            }
        )
        result = await voice_routes.audio_models(
            request=request,
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None),
        )

        headers: Final = dict(result.headers)
        assert headers.get(voice_routes.VOICE_RESOLUTION_HEADER) == "explicit"
        assert headers.get(voice_routes.VOICE_RESOLVED_MODEL_HEADER) == "drama3"


class TestAudioModelsProviderScope:
    """?provider= on /v1/audio/models must scope the pod and must not leak the
    selection key back to the pod as a query parameter it cannot understand."""

    def _request(self, query: bytes = b"provider=drama3") -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/v1/audio/models",
                "headers": Headers({}).raw,
                "query_string": query,
            }
        )

    def _patch(
        self, monkeypatch: pytest.MonkeyPatch, models: list[dict], captured: dict[str, Any]
    ) -> None:
        monkeypatch.setattr(
            "litellm.proxy.proxy_server.llm_router",
            _FakeRouter(models),
            raising=False,
        )
        import httpx as _httpx

        class _Resp:
            def __init__(self, content: bytes = b"{}", code: int = 200) -> None:
                self.content = content
                self.status_code = code
                self.headers: dict[str, str] = {"content-type": "application/json"}

        class _Client:
            def __init__(self, **kwargs: Any) -> None:
                captured["init_kwargs"] = kwargs

            async def __aenter__(self) -> Any:
                return self

            async def __aexit__(self, *_: Any) -> None:
                pass

            async def get(self, *args: Any, **kwargs: Any) -> Any:
                captured["get_args"] = args
                captured["get_kwargs"] = kwargs
                return _Resp()

        monkeypatch.setattr(_httpx, "AsyncClient", _Client)

    @pytest.mark.asyncio
    async def test_provider_scopes_the_target_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models: Final = [
            {"model_name": "omnivoice", "litellm_params": {"model": "omnivoice/omnivoice", "mode": "audio_speech", "api_base": "http://omni:8080/v1"}},
            {"model_name": "drama3", "litellm_params": {"model": "drama3/drama-3", "mode": "audio_speech", "api_base": "http://drama:8080/v1"}},
        ]
        captured: dict[str, Any] = {}
        self._patch(monkeypatch, models, captured)

        await voice_routes.audio_models(
            request=self._request(),
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None),
        )

        # The request was scoped to drama3, so it must have hit drama3's base.
        assert captured["get_args"][0] == "http://drama:8080/v1/models"

    @pytest.mark.asyncio
    async def test_provider_is_not_forwarded_as_a_query_param(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        models: Final = [
            {"model_name": "drama3", "litellm_params": {"model": "drama3/drama-3", "mode": "audio_speech", "api_base": "http://drama:8080/v1"}},
        ]
        captured: dict[str, Any] = {}
        self._patch(monkeypatch, models, captured)

        await voice_routes.audio_models(
            request=self._request(),
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None),
        )

        forwarded: Final = captured["get_kwargs"].get("params") or {}
        assert "provider" not in forwarded


def _patched_route(monkeypatch: pytest.MonkeyPatch, pod_response: dict) -> None:
    """Replace the network edges of a voice route; the router-injected metadata stays on the dict."""

    async def _route_request(**kwargs: Any) -> Any:
        async def _completed() -> dict:
            pod_response.setdefault("_hidden_params", _HIDDEN["_hidden_params"])
            return pod_response

        return _completed()

    async def _identity(**kwargs: Any) -> Any:
        return kwargs["data"]

    monkeypatch.setattr(voice_routes, "route_request", _route_request)
    monkeypatch.setattr(voice_routes, "add_litellm_data_to_request", _identity)

    logging_obj = MagicMock()
    logging_obj.pre_call_hook = AsyncMock(side_effect=lambda **kwargs: kwargs["data"])
    logging_obj.update_request_status = AsyncMock()
    logging_obj.post_call_failure_hook = AsyncMock()
    logging_obj.post_call_response_headers_hook = AsyncMock(return_value={})
    monkeypatch.setattr(proxy_server, "proxy_logging_obj", logging_obj)
    monkeypatch.setattr(proxy_server, "user_model", None, raising=False)
    monkeypatch.setattr(proxy_server, "llm_router", None, raising=False)
    monkeypatch.setattr(proxy_server, "general_settings", {}, raising=False)
    monkeypatch.setattr(proxy_server, "version", "0.0.0-test", raising=False)
    monkeypatch.setattr(proxy_server, "proxy_config", None, raising=False)


def _body_of(result: object) -> dict:
    import orjson

    assert isinstance(result, JSONResponse)
    return orjson.loads(bytes(result.body))


class TestHiddenParamsNeverReachVoiceClients:
    """The router attaches ``_hidden_params`` (model ids, call ids, routing
    headers) to every dict response via ``set_response_headers``. The voice
    routes return those dicts verbatim, so the metadata leaked to clients in
    list, upload and delete responses."""

    @pytest.mark.asyncio
    async def test_list_voices_strips_hidden_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_route(monkeypatch, {"voices": ["default"], "uploaded_voices": []})

        result = await voice_routes.list_voices(
            request=_mnemonic_request("/v1/voices"),
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region=""),
        )

        body = _body_of(result)
        assert "_hidden_params" not in body
        assert body["voices"] == ["default"]

    @pytest.mark.asyncio
    async def test_voice_profile_actions_strip_hidden_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_route(monkeypatch, {"voices": []})

        result = await voice_routes.list_voice_profiles(
            request=_mnemonic_request("/v1/voices/profiles"),
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region=""),
        )

        assert "_hidden_params" not in _body_of(result)

    @pytest.mark.asyncio
    async def test_delete_profile_strips_hidden_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_route(monkeypatch, {"success": True, "message": "Voice 'x' deleted successfully"})

        result = await voice_routes.delete_voice_profile(
            request=_mnemonic_request("/v1/voices/profiles/x"),
            fastapi_response=MagicMock(),
            profile_id="x",
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region=""),
        )

        assert "_hidden_params" not in _body_of(result)

    @pytest.mark.asyncio
    async def test_create_voice_upload_strips_hidden_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_route(monkeypatch, {"name": "Narrator_01", "consent": "ok"})

        result = await voice_routes.create_voice(
            request=_mnemonic_request("/v1/audio/voices"),
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region=""),
        )

        body = _body_of(result)
        assert "_hidden_params" not in body
        assert body["name"] == "Narrator_01"

    @pytest.mark.asyncio
    async def test_audio_script_dict_branch_strips_hidden_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patched_route(monkeypatch, {"script": []})
        payload: Final = b'{"model": "omnivoice", "script": []}'

        result = await voice_routes.audio_script(
            request=_mnemonic_request("/v1/audio/script", payload),
            fastapi_response=MagicMock(),
            user_api_key_dict=MagicMock(user_id=None, allowed_model_region=""),
        )

        assert isinstance(result, JSONResponse)
        assert "_hidden_params" not in _body_of(result)
