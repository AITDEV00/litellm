"""Behavior pins for ``litellm/proxy/audio_translation_routes.py``.

The route is the OpenAI ``POST /v1/audio/translations`` surface: multipart
``file`` + form fields, dynamic model resolution, and the shared audio response
adapter. These tests pin the observable HTTP contract on both alias paths:
happy-path JSON, provider extension fields (``target_language``) reaching the
dispatched call, truthful upstream 400s for unsupported streaming, missing-file
400, and error envelopes.

The dispatched kwargs are captured with a real ``Router`` instance whose
``atranslation`` is an injected fake, so the test proves the route's contract
with the router (deployment-injected extras like ``keepalive_seconds`` must
not be forwarded) without patching class internals.
"""

from __future__ import annotations

import io
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock

import pytest

from litellm.proxy import audio_translation_routes, proxy_server
from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse
from litellm.types.utils import TranscriptionResponse

_AUDIO: Final = b"\x00\x01\x02RIFF"
_FILES: Final = {"file": ("meeting.mp3", _AUDIO, "audio/mpeg")}


class _CapturedRouter:
    """A real-enough router: captures dispatch kwargs, returns a canned response."""

    def __init__(self, response: Any = None, exc: Exception | None = None) -> None:
        self.model_names: list[str] = ["nemotron-audio"]
        self.calls: list[dict[str, Any]] = []
        self._response = response
        self._exc = exc

    async def atranslation(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response


def _patch_common(monkeypatch: pytest.MonkeyPatch, router: _CapturedRouter) -> None:
    monkeypatch.setattr(proxy_server, "llm_router", router)
    monkeypatch.setattr(proxy_server, "user_model", None, raising=False)
    monkeypatch.setattr(
        proxy_server,
        "proxy_logging_obj",
        MagicMock(
            pre_call_hook=AsyncMock(side_effect=lambda **kw: kw["data"]),
            post_call_failure_hook=AsyncMock(),
            post_call_response_headers_hook=AsyncMock(return_value={}),
            update_request_status=AsyncMock(),
        ),
    )
    monkeypatch.setattr(
        audio_translation_routes,
        "route_request",
        AsyncMock(side_effect=lambda **kwargs: router.atranslation(**kwargs["data"])),
    )

    async def _add_data(data: dict, **kwargs: Any) -> dict:
        return data

    monkeypatch.setattr(audio_translation_routes, "add_litellm_data_to_request", _add_data)
    monkeypatch.setattr(audio_translation_routes, "check_file_size_under_limit", lambda **kwargs: True)
    monkeypatch.setattr(proxy_server, "general_settings", {}, raising=False)


@pytest.fixture
def captured_router() -> _CapturedRouter:
    return _CapturedRouter(response=TranscriptionResponse(text="hello translated"))


@pytest.fixture
def patched_route(monkeypatch: pytest.MonkeyPatch, captured_router: _CapturedRouter) -> _CapturedRouter:
    _patch_common(monkeypatch, captured_router)
    return captured_router


@pytest.fixture
def app():
    from litellm.proxy.proxy_server import app as _app

    return _app


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_as(app):
    import contextlib

    from litellm.proxy._types import LitellmUserRoles, UserAPIKeyAuth
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

    @contextlib.contextmanager
    def _auth_as(role: Any = LitellmUserRoles.PROXY_ADMIN) -> Any:
        fake_auth = UserAPIKeyAuth(
            api_key="sk-test-key",
            user_id="test-user-id",
            user_role=role,
        )

        async def _override() -> UserAPIKeyAuth:
            return fake_auth

        app.dependency_overrides[user_api_key_auth] = _override
        try:
            yield fake_auth
        finally:
            app.dependency_overrides.pop(user_api_key_auth, None)

    return _auth_as


class TestHappyPath:
    @pytest.mark.parametrize("path", ["/v1/audio/translations", "/audio/translations"])
    def test_returns_native_json_body(
        self, client, auth_as, patched_route, path: str
    ) -> None:
        with auth_as():
            response = client.post(path, files=_FILES, data={"model": "nemotron-audio"})
        assert response.status_code == 200
        body = response.json()
        assert body["text"] == "hello translated"
        assert "usage" in body, "declared-but-unset fields stay in the public JSON (parity with STT)"
        assert response.headers.get("content-type", "").split(";")[0] == "application/json"

    @pytest.mark.parametrize("path", ["/v1/audio/translations", "/audio/translations"])
    def test_custom_headers_ride_the_returned_response(
        self, client, auth_as, patched_route, path: str
    ) -> None:
        """The route returns an explicit Response, so FastAPI never merges the
        injected response parameter's headers; the built custom headers must ride
        the returned Response or every translation loses its x-litellm headers."""
        with auth_as():
            response = client.post(path, files=_FILES, data={"model": "nemotron-audio"})
        assert response.status_code == 200
        assert response.headers.get("x-litellm-call-id") is not None
        assert response.headers["x-litellm-call-id"] != ""

    def test_target_language_reaches_the_router_dispatch(
        self, client, auth_as, patched_route
    ) -> None:
        with auth_as():
            response = client.post(
                "/v1/audio/translations",
                files=_FILES,
                data={"model": "nemotron-audio", "target_language": "en"},
            )
        assert response.status_code == 200
        dispatched = patched_route.calls[0]
        assert dispatched["target_language"] == "en"
        assert dispatched["model"] == "nemotron-audio"
        assert isinstance(dispatched["file"], io.BytesIO)
        assert dispatched["file"].name == "meeting.mp3"

    def test_deployment_injected_extras_are_not_forwarded_by_the_route(
        self, client, auth_as, patched_route
    ) -> None:
        """keepalive_seconds arrives via deployment merge, not the form; the route must not add it."""
        with auth_as():
            response = client.post("/v1/audio/translations", files=_FILES, data={"model": "nemotron-audio"})
        assert response.status_code == 200
        assert "keepalive_seconds" not in patched_route.calls[0]

    def test_true_document_body_and_media_type(self, client, auth_as, patched_route) -> None:
        patched_route._response = NativeAudioDocumentResponse(
            body="1\n00:00:00,000 --> 00:00:01,000\nhello\n",
            media_type="application/x-subrip",
        )
        with auth_as():
            response = client.post("/v1/audio/translations", files=_FILES, data={"model": "nemotron-audio"})
        assert response.status_code == 200
        assert response.headers["content-type"].split(";")[0] == "application/x-subrip"
        assert "hello" in response.text
        assert b'{"text"' not in response.content


class TestErrors:
    def test_missing_file_is_rejected(self, client, auth_as, patched_route) -> None:
        """FastAPI validates the required multipart part before the handler, same as STT."""
        with auth_as():
            response = client.post("/v1/audio/translations", data={"model": "nemotron-audio"})
        assert response.status_code == 422
        assert patched_route.calls == []

    def test_upstream_unsupported_stream_400_is_truthful(
        self, client, auth_as, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import litellm

        router = _CapturedRouter(
            exc=litellm.BadRequestError(
                message="Streaming is not supported for audio translations on this deployment",
                model="nemotron-audio",
                llm_provider="nvidia_nim",
            )
        )
        _patch_common(monkeypatch, router)

        with auth_as():
            response = client.post(
                "/v1/audio/translations",
                files=_FILES,
                data={"model": "nemotron-audio", "stream": "true"},
            )
        assert response.status_code == 400
        assert "Streaming is not supported" in response.json()["error"]["message"]
        assert router.calls, "the request must still reach the router before the truthful 400"

    def test_generic_failure_is_wrapped_not_swallowed(
        self, client, auth_as, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        router = _CapturedRouter(exc=ValueError("translation boom"))
        _patch_common(monkeypatch, router)

        with auth_as():
            response = client.post("/v1/audio/translations", files=_FILES, data={"model": "nemotron-audio"})
        assert response.status_code == 500
        assert "translation boom" in response.json()["error"]["message"]

    def test_llm_api_scoped_key_can_call_both_alias_paths(self) -> None:
        """A key scoped to allowed_routes=['llm_api_routes'] must reach the route.

        The live 401/403 proof runs against the dev gateway in the integration
        suite; here we pin the same authorization fact through RouteChecks,
        which is the check user_api_key_auth delegates to.
        """
        from unittest.mock import MagicMock

        from fastapi import Request as FastAPIRequest

        from litellm.proxy._types import UserAPIKeyAuth
        from litellm.proxy.auth.route_checks import RouteChecks

        token = UserAPIKeyAuth(user_id="guard", allowed_routes=["llm_api_routes"])
        request = MagicMock(spec=FastAPIRequest)
        request.method = "POST"
        for path in ("/audio/translations", "/v1/audio/translations"):
            assert RouteChecks.is_virtual_key_allowed_to_call_route(
                route=path, valid_token=token, request=request
            ), f"a llm_api key must reach {path}"
