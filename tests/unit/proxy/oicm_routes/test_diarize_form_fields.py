"""Form-field forwarding for ``POST /v1/diarize`` (litellm/proxy/diarization_routes.py).

The gateway forwards every multipart field except ``file`` and ``model``, which
is what lets opt-in pod flags such as ``embeddings`` reach the engine without a
gateway-side allowlist. Only the spec fields in ``_KNOWN_LITELLM_PARAMS`` are
read by the gateway itself. These tests pin that split, because narrowing the
forwarded set silently strips flags and the default response still looks valid.
"""

import io
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.datastructures import Headers, UploadFile
from starlette.requests import Request

from litellm.proxy import diarization_routes, proxy_server

_AUDIO: Final = b"RIFF....WAVEfmt "


class _AudioUpload(UploadFile):
    def __init__(self) -> None:
        super().__init__(file=io.BytesIO(_AUDIO), filename="meeting.wav", headers=Headers({}))

    @property
    def content_type(self) -> str:
        return "audio/wav"


def _multipart_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/diarize",
            "headers": Headers({"content-type": "multipart/form-data; boundary=test"}).raw,
            "query_string": b"",
        }
    )


def _patched_route(monkeypatch: pytest.MonkeyPatch, form: dict[str, Any]) -> dict[str, Any]:
    """Run the route handler with the network edges replaced; return the dispatched data."""
    captured: dict[str, Any] = {}

    async def _capture_route_request(**kwargs: Any) -> Any:
        captured.update(kwargs["data"])

        async def _completed_job() -> dict[str, Any]:
            return {"status": "succeeded", "output": {}}

        return _completed_job()

    monkeypatch.setattr(diarization_routes, "get_form_data", AsyncMock(return_value=form))

    async def _identity(**kwargs: Any) -> Any:
        return kwargs["data"]

    monkeypatch.setattr(diarization_routes, "add_litellm_data_to_request", _identity)
    monkeypatch.setattr(diarization_routes, "route_request", _capture_route_request)

    logging_obj = MagicMock()
    logging_obj.pre_call_hook = AsyncMock(side_effect=lambda **kwargs: kwargs["data"])
    logging_obj.update_request_status = AsyncMock()
    logging_obj.post_call_failure_hook = AsyncMock()
    monkeypatch.setattr(proxy_server, "proxy_logging_obj", logging_obj)
    monkeypatch.setattr(proxy_server, "user_model", None, raising=False)

    return captured


async def _invoke(monkeypatch: pytest.MonkeyPatch, form: dict[str, Any]) -> dict[str, Any]:
    captured = _patched_route(monkeypatch, form)
    await diarization_routes.diarize(
        request=_multipart_request(),
        fastapi_response=MagicMock(),
        user_api_key_dict=MagicMock(user_id=None, allowed_model_region=""),
    )
    return captured


class TestFormFieldForwarding:
    @pytest.mark.asyncio
    async def test_embeddings_reaches_the_pod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dispatched = await _invoke(
            monkeypatch,
            {"model": "pyannote/community-1", "embeddings": "true", "file": _AudioUpload()},
        )
        assert dispatched["form_fields"] == {"embeddings": "true"}

    @pytest.mark.asyncio
    async def test_flag_absence_forwarded_as_absence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dispatched = await _invoke(
            monkeypatch,
            {"model": "pyannote/community-1", "file": _AudioUpload()},
        )
        assert "embeddings" not in dispatched["form_fields"]

    @pytest.mark.asyncio
    async def test_file_and_model_never_leak_into_form_fields(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dispatched = await _invoke(
            monkeypatch,
            {
                "model": "pyannote/community-1",
                "exclusive": "true",
                "file": _AudioUpload(),
            },
        )
        assert set(dispatched["form_fields"]) == {"exclusive"}

    @pytest.mark.asyncio
    async def test_unknown_fields_are_forwarded_not_dropped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The pod ignores undeclared fields, so forwarding them keeps its contract the single source of truth."""
        dispatched = await _invoke(
            monkeypatch,
            {"model": "pyannote/community-1", "zzz": "1", "file": _AudioUpload()},
        )
        assert dispatched["form_fields"] == {"zzz": "1"}

    @pytest.mark.asyncio
    async def test_client_supplied_model_wins_over_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dispatched = await _invoke(
            monkeypatch,
            {"model": "pyannote/community-1", "file": _AudioUpload()},
        )
        assert dispatched["model"] == "pyannote/community-1"


class TestEmbeddingsIsNotAGatewayParam:
    def test_embeddings_absent_from_known_litellm_params(self) -> None:
        """``_KNOWN_LITELLM_PARAMS`` gates what the gateway reads for logging and model
        resolution. ``embeddings`` is pod-side only, so adding it there would change
        nothing on the wire, and this pin makes that boundary explicit."""
        assert "embeddings" not in diarization_routes._KNOWN_LITELLM_PARAMS
