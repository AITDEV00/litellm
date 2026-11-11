import asyncio
from collections.abc import AsyncIterator
from typing import Final
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import Request, Response
from starlette.responses import StreamingResponse

from litellm.constants import AUDIO_SPEECH_CHUNK_SIZE
from litellm.proxy import proxy_server
from litellm.proxy.voice_routes import _audio_media_type, audio_speech_clone
from litellm.types.llms.openai import HttpxBinaryResponseContent


@pytest.mark.parametrize(
    ("data", "upstream_content_type", "expected"),
    (
        ({"response_format": "wav"}, "audio/mpeg; charset=binary", "audio/wav"),
        ({"response_format": "pcm"}, "audio/wav", "audio/pcm"),
        ({"response_format": "mulaw"}, "audio/wav", "audio/basic"),
        ({"response_format": "wav"}, "audio/flac", "audio/flac"),
        ({"response_format": "wav"}, None, "audio/wav"),
    ),
)
def test_audio_media_type_preserves_transcoding_and_honors_native_format(data, upstream_content_type, expected):
    response = HttpxBinaryResponseContent(
        httpx.Response(
            200, content=b"RIFF", headers={"content-type": upstream_content_type} if upstream_content_type else {}
        )
    )
    if upstream_content_type != "audio/flac":
        response.hidden_params["response_format"] = data["response_format"]

    assert _audio_media_type(data, response) == expected


class _Logger:
    async def pre_call_hook(self, **kwargs):
        return kwargs["data"]

    async def post_call_response_headers_hook(self, **kwargs):
        return {}

    async def update_request_status(self, **kwargs):
        return None

    async def post_call_failure_hook(self, **kwargs):
        raise AssertionError("The audio route must not fail")


@pytest.mark.asyncio
async def test_wav_clone_returns_wav_media_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proxy_server, "general_settings", {}, raising=False)
    monkeypatch.setattr(proxy_server, "proxy_config", {}, raising=False)
    monkeypatch.setattr(proxy_server, "user_model", None, raising=False)
    monkeypatch.setattr(proxy_server, "llm_router", None, raising=False)
    monkeypatch.setattr(proxy_server, "proxy_logging_obj", _Logger())

    async def add_request_data(**kwargs):
        return kwargs["data"]

    async def llm_call():
        return HttpxBinaryResponseContent(httpx.Response(200, content=b"RIFF\x04\x00\x00\x00WAVE"))

    async def route_request(**kwargs):
        assert kwargs["data"]["response_format"] == "wav"
        return llm_call()

    monkeypatch.setattr("litellm.proxy.voice_routes.add_litellm_data_to_request", add_request_data)
    monkeypatch.setattr("litellm.proxy.voice_routes.route_request", route_request)
    boundary = "test-multipart-boundary"
    body = (
        b"--test-multipart-boundary\r\n"
        b'Content-Disposition: form-data; name="model"\r\n\r\nomnivoice\r\n'
        b"--test-multipart-boundary\r\n"
        b'Content-Disposition: form-data; name="text"\r\n\r\nhello\r\n'
        b"--test-multipart-boundary\r\n"
        b'Content-Disposition: form-data; name="response_format"\r\n\r\nwav\r\n'
        b"--test-multipart-boundary\r\n"
        b'Content-Disposition: form-data; name="ref_audio"; filename="fixture.wav"\r\n'
        b"Content-Type: audio/wav\r\n\r\nRIFF\x04\x00\x00\x00WAVE\r\n"
        b"--test-multipart-boundary--\r\n"
    )

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    response = await audio_speech_clone(
        Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "path": "/v1/audio/speech/clone",
                "root_path": "",
                "scheme": "http",
                "query_string": b"",
                "client": ("127.0.0.1", 12345),
                "server": ("test", 80),
                "headers": [(b"content-type", f"multipart/form-data; boundary={boundary}".encode())],
            },
            receive,
        ),
        Response(),
        MagicMock(user_id=None),
    )

    assert isinstance(response, StreamingResponse)
    assert response.media_type == "audio/wav"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("upstream_content_type", "native_format", "expected_media_type"),
    (("audio/wav", "pcm", "audio/pcm"), ("audio/wav", None, "audio/wav")),
)
async def test_speech_route_uses_guaranteed_native_pcm_instead_of_an_upstream_wav_label(
    upstream_content_type: str, native_format: str | None, expected_media_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream_response = HttpxBinaryResponseContent(
        httpx.Response(200, content=b"\x00\x01\xff\xfe", headers={"content-type": upstream_content_type})
    )
    if native_format is not None:
        upstream_response.hidden_params["response_format"] = native_format

    async def serve_pcm(**kwargs):
        return upstream_response

    async def route_request(**kwargs):
        assert kwargs["data"]["response_format"] == "pcm"
        return serve_pcm()

    async def add_request_data(**kwargs):
        return kwargs["data"]

    monkeypatch.setattr(proxy_server, "proxy_logging_obj", _Logger())
    monkeypatch.setattr(proxy_server, "user_model", None)
    monkeypatch.setattr(proxy_server, "add_litellm_data_to_request", add_request_data)
    monkeypatch.setattr(proxy_server, "route_request", route_request)

    async def receive():
        return {
            "type": "http.request",
            "body": b'{"model":"hamsa-tts-new","response_format":"pcm","stream":true}',
            "more_body": False,
        }

    response = await proxy_server.audio_speech(
        Request({"type": "http", "headers": [], "method": "POST", "path": "/v1/audio/speech"}, receive),
        Response(),
        MagicMock(user_id=None, allowed_model_region=""),
    )
    assert isinstance(response, StreamingResponse)
    assert response.media_type == expected_media_type


class _DelayedAudioStream(httpx.AsyncByteStream):
    def __init__(
        self,
        chunks: tuple[bytes, ...],
        release: asyncio.Event | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.chunks = chunks
        self.release = release
        self.failure = failure
        self.second_chunk_requested = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self.chunks):
            if index == 1:
                self.second_chunk_requested.set()
                if self.failure is not None:
                    raise self.failure
                if self.release is not None:
                    await self.release.wait()
            yield chunk

    async def aclose(self) -> None:
        self.closed.set()


def _delayed_binary_response(
    chunks: tuple[bytes, ...], content_type: str, release: asyncio.Event | None = None
) -> HttpxBinaryResponseContent:
    return HttpxBinaryResponseContent(
        httpx.Response(200, headers={"content-type": content_type}, stream=_DelayedAudioStream(chunks, release))
    )


@pytest.mark.asyncio
@pytest.mark.timeout(5)
async def test_speech_generator_yields_before_releasing_the_next_upstream_frame() -> None:
    release: Final = asyncio.Event()
    first_chunk: Final = b"a" * AUDIO_SPEECH_CHUNK_SIZE
    stream: Final = _DelayedAudioStream((first_chunk, b"second"), release)
    response: Final = HttpxBinaryResponseContent(
        httpx.Response(200, headers={"content-type": "audio/wav"}, stream=stream)
    )
    iterator: Final = proxy_server._audio_speech_chunk_generator(response)

    first: Final = await asyncio.wait_for(anext(iterator), timeout=0.1)
    assert first == first_chunk
    assert not release.is_set(), "The first yield must not wait for the gated upstream frame"

    release.set()
    assert await asyncio.wait_for(anext(iterator), timeout=0.1) == b"second"
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert stream.closed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ("cancel", "fail"))
@pytest.mark.timeout(5)
async def test_speech_generator_closes_upstream_when_interrupted(interruption: str) -> None:
    stream: Final = _DelayedAudioStream(
        (b"a" * AUDIO_SPEECH_CHUNK_SIZE, b"never delivered"),
        release=asyncio.Event() if interruption == "cancel" else None,
        failure=RuntimeError("upstream audio failed") if interruption == "fail" else None,
    )
    response: Final = HttpxBinaryResponseContent(
        httpx.Response(200, headers={"content-type": "audio/wav"}, stream=stream)
    )
    first_chunk: Final = b"a" * AUDIO_SPEECH_CHUNK_SIZE
    iterator: Final = proxy_server._audio_speech_chunk_generator(response)
    assert await asyncio.wait_for(anext(iterator), timeout=0.1) == first_chunk

    if interruption == "cancel":
        consumer: Final = asyncio.create_task(anext(iterator))
        await asyncio.wait_for(stream.second_chunk_requested.wait(), timeout=0.1)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    else:
        with pytest.raises(RuntimeError, match="upstream audio failed"):
            await anext(iterator)

    assert response.response.is_closed
    assert stream.closed.is_set()


def _speech_request(body: bytes) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "headers": [], "method": "POST", "path": "/v1/audio/speech"},
        receive,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream_format", ("sse", None))
@pytest.mark.timeout(5)
async def test_speech_route_streams_delayed_frames_with_their_wire_media_type(
    stream_format: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    release: Final = asyncio.Event()
    chunks: Final = (
        (
            b'data: {"type":"speech.audio.delta","audio":"AAAA"}\n\n',
            b'data: {"type":"speech.audio.done"}\n\n',
        )
        if stream_format == "sse"
        else (b"RIFF" + b"a" * (AUDIO_SPEECH_CHUNK_SIZE - 4), b"b" * AUDIO_SPEECH_CHUNK_SIZE)
    )
    upstream_content_type: Final = "application/json" if stream_format == "sse" else "audio/flac"
    response: Final = _delayed_binary_response(chunks, upstream_content_type, release)

    async def route_request(**kwargs):
        assert kwargs["data"].get("stream_format") == stream_format

        async def llm_call():
            return response

        return llm_call()

    async def add_request_data(**kwargs):
        return kwargs["data"]

    monkeypatch.setattr(proxy_server, "proxy_logging_obj", _Logger())
    monkeypatch.setattr(proxy_server, "user_model", None)
    monkeypatch.setattr(proxy_server, "add_litellm_data_to_request", add_request_data)
    monkeypatch.setattr(proxy_server, "route_request", route_request)
    payload: Final = (
        b'{"model":"fishaudio/drama-3","response_format":"pcm","stream":true,"stream_format":"sse"}'
        if stream_format == "sse"
        else b'{"model":"hamsa-tts-new","response_format":"wav","stream":true}'
    )

    result: Final = await proxy_server.audio_speech(_speech_request(payload), Response(), MagicMock(user_id=None))

    assert isinstance(result, StreamingResponse)
    assert result.media_type == ("text/event-stream" if stream_format == "sse" else "audio/flac")
    assert result.headers["content-type"] == (
        "text/event-stream; charset=utf-8" if stream_format == "sse" else "audio/flac"
    )
    body_iterator: Final = result.body_iterator
    stream: Final = response.response.stream
    assert isinstance(stream, _DelayedAudioStream)

    assert await asyncio.wait_for(anext(body_iterator), timeout=0.1) == chunks[0]
    second_read: Final = asyncio.create_task(anext(body_iterator))
    await asyncio.wait_for(stream.second_chunk_requested.wait(), timeout=0.1)
    assert not second_read.done(), "The next upstream event must remain gated"
    release.set()
    assert await asyncio.wait_for(second_read, timeout=0.1) == chunks[1]
    with pytest.raises(StopAsyncIteration):
        await anext(body_iterator)

    assert response.response.is_closed
    assert stream.closed.is_set()
