"""Regression tests for TTS upstream streaming dispatch.

A provider request body with "stream": true must reach the pod verbatim AND the
upstream call must not buffer the audio: the first bytes the client sees must be
the pod's first synthesized chunk, not the end of a fully-downloaded body. A
buffered call reports time_starttransfer ~= time_total; a streaming call
relays the pod's ~300 ms first chunk.
"""

from typing import Final
from unittest.mock import Mock

import httpx
import pytest
from httpx._content import AsyncIteratorByteStream

from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.llms.hamsa.text_to_speech.transformation import HamsaTextToSpeechConfig

_POD_HEADERS: Final = {"content-type": "audio/wav"}
_FIRST_CHUNK: Final = b"\x01\x00\x02\x00"


def _pod_stream_response() -> httpx.Response:
    """A response whose body is a lazy async iterator: unread until iterated."""

    async def _gen():
        yield _FIRST_CHUNK

    return httpx.Response(200, headers=_POD_HEADERS, stream=AsyncIteratorByteStream(_gen()))


def _hamsa_call_kwargs(stream: bool) -> dict:
    config = HamsaTextToSpeechConfig()
    speaker, mapped = config.map_openai_params(
        model="hamsa-tts-new",
        optional_params={},
        voice="Ruba",
        drop_params=False,
        kwargs={"stream": stream},
    )
    request_data = config.transform_text_to_speech_request(
        model="hamsa-tts-new",
        input="hello",
        voice=speaker,
        optional_params=dict(mapped),
        litellm_params={"api_base": "http://pod:8080"},
        headers={},
    )
    assert request_data["dict_body"]["stream"] is stream
    return {
        "model": "hamsa-tts-new",
        "input": "hello",
        "voice": speaker,
        "text_to_speech_provider_config": config,
        "text_to_speech_optional_params": dict(mapped),
        "custom_llm_provider": "hamsa",
        "litellm_params": {"api_base": "http://pod:8080", "api_key": ""},
        "logging_obj": Mock(),
        "timeout": 30,
        "extra_headers": None,
    }


def _handler():
    from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

    return BaseLLMHTTPHandler()


@pytest.mark.asyncio
async def test_async_tts_stream_true_does_not_buffer() -> None:
    """stream: true must produce an unread upstream response the proxy relays chunk by chunk."""
    seen: dict = {}
    transport = httpx.MockTransport(lambda request: _pod_stream_response())
    wrapper = AsyncHTTPHandler()
    raw_client = httpx.AsyncClient(transport=transport)
    wrapper.client = raw_client
    original_send = raw_client.send

    async def spying_send(request, **kwargs):
        seen["stream_kwarg"] = kwargs.get("stream")
        return await original_send(request, **kwargs)

    raw_client.send = spying_send  # type: ignore[method-assign]

    response = await _handler().async_text_to_speech_handler(
        client=wrapper,
        **_hamsa_call_kwargs(stream=True),
    )
    assert seen["stream_kwarg"] is True, "upstream send must be stream=True when the pod body asks for it"
    assert response.response.is_stream_consumed is False, "handler must not read the body eagerly"
    assert await response.aread() == _FIRST_CHUNK
    await raw_client.aclose()


@pytest.mark.asyncio
async def test_async_tts_stream_false_still_buffers() -> None:
    """Default (no stream) keeps the existing fully-buffered WAV behavior."""
    seen: dict = {}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers=_POD_HEADERS, content=b"RIFFxxxxWAVE")
    )
    wrapper = AsyncHTTPHandler()
    raw_client = httpx.AsyncClient(transport=transport)
    wrapper.client = raw_client
    original_send = raw_client.send

    async def spying_send(request, **kwargs):
        seen["stream_kwarg"] = kwargs.get("stream")
        return await original_send(request, **kwargs)

    raw_client.send = spying_send  # type: ignore[method-assign]

    response = await _handler().async_text_to_speech_handler(
        client=wrapper,
        **_hamsa_call_kwargs(stream=False),
    )
    assert seen["stream_kwarg"] is False
    assert response.response.is_stream_consumed is True, "buffered behavior must be unchanged"
    assert response.content == b"RIFFxxxxWAVE"
    await raw_client.aclose()


def test_sync_tts_stream_true_does_not_buffer() -> None:
    seen: dict = {}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers=_POD_HEADERS, content=iter([_FIRST_CHUNK]))
    )
    wrapper = HTTPHandler()
    raw_client = httpx.Client(transport=transport)
    wrapper.client = raw_client
    original_send = raw_client.send

    def spying_send(request, **kwargs):
        seen["stream_kwarg"] = kwargs.get("stream")
        return original_send(request, **kwargs)

    raw_client.send = spying_send  # type: ignore[method-assign]

    response = _handler().text_to_speech_handler(
        client=wrapper,
        _is_async=False,
        **_hamsa_call_kwargs(stream=True),
    )
    assert seen["stream_kwarg"] is True
    assert response.read() == _FIRST_CHUNK
    raw_client.close()


def test_sync_tts_stream_false_still_buffers() -> None:
    seen: dict = {}
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, headers=_POD_HEADERS, content=b"RIFFxxxxWAVE")
    )
    wrapper = HTTPHandler()
    raw_client = httpx.Client(transport=transport)
    wrapper.client = raw_client
    original_send = raw_client.send

    def spying_send(request, **kwargs):
        seen["stream_kwarg"] = kwargs.get("stream")
        return original_send(request, **kwargs)

    raw_client.send = spying_send  # type: ignore[method-assign]

    response = _handler().text_to_speech_handler(
        client=wrapper,
        _is_async=False,
        **_hamsa_call_kwargs(stream=False),
    )
    assert seen["stream_kwarg"] is False
    assert response.read() == b"RIFFxxxxWAVE"
    raw_client.close()
