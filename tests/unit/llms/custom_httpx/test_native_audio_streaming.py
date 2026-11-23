"""Behavior tests for the native multipart transcript SSE stream lifecycle.

Pins the plan's C2/C3/C4 contracts through the real code paths with an
injectable fake transport (handshake and delayed frames without a network):

- frames cut at arbitrary network boundaries decode to the same events
- a delta always precedes the terminal done; done carries the native usage
- exactly one terminal event: a second done is an error, and a stream that
  ends with no terminal surfaces a structured error event
- success/failure logging dispatches exactly once; repeated disposal never
  double-logs
- caller disconnect (cancellation) closes the upstream transport with no
  terminal logging
- the proxy serializer emits deterministic SSE frames with the native event
  vocabulary preserved, and unknown vendor keys survive
"""

import asyncio
import io
import json
from collections.abc import AsyncIterator
from typing import Optional
from unittest.mock import MagicMock

import httpx
import pytest

from litellm.llms.base_llm.audio_transcription.streaming import (
    SharedNativeTranscriptionStream,
    SyncNativeTranscriptionStream,
)
from litellm.llms.nvidia_nim.audio_transcription.transformation import (
    NvidiaNimAudioTranscriptionConfig,
)
from litellm.proxy.audio_response_helpers import serialize_native_transcript_event
from litellm.types.llms.audio_transcription import (
    NativeTranscriptDelta,
    NativeTranscriptDone,
    NativeTranscriptStreamError,
    OpenedNativeAudioStream,
)


def _hosted_vllm_config():
    from litellm.llms.hosted_vllm.transcriptions.transformation import (
        HostedVLLMAudioTranscriptionConfig,
    )

    return HostedVLLMAudioTranscriptionConfig()


_HOSTED_VLLM_CONFIG = _hosted_vllm_config

_NIM_CONFIG: Optional[NvidiaNimAudioTranscriptionConfig] = None


def _config() -> NvidiaNimAudioTranscriptionConfig:
    return NvidiaNimAudioTranscriptionConfig()


def _live_vocabulary_frames() -> bytes:
    """The event shapes verified against the live NIM transcribe service."""
    frames = [
        'event: transcript.text.delta\ndata: {"delta":"Four","type":"transcript.text.delta"}\n\n',
        'event: transcript.text.delta\ndata: {"delta":" score and seven","type":"transcript.text.delta"}\n\n',
        "event: transcript.text.done\n"
        + 'data: {"text":"Four score and seven","transcription":{"duration":17.5},"type":"transcript.text.done","usage":{"seconds":17.5,"prompt_tokens":272}}'
        + "\n\n",
    ]
    return "".join(frames).encode()


class _FakeAsyncTransport:
    """Async SSE transport that hands out bytes in fixed chunk sizes."""

    def __init__(self, payload: bytes, chunk_size: int = 7, delay: float = 0.0):
        self._chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]
        self._delay = delay
        self.closed = False
        self.close_count = 0

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            if self._delay:
                await asyncio.sleep(self._delay)
            yield chunk

    async def aclose(self) -> None:
        self.close_count += 1
        self.closed = True


class _FakeSyncTransport:
    def __init__(self, payload: bytes, chunk_size: int = 7):
        self._chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]
        self.close_count = 0

    def iter_bytes(self):
        yield from self._chunks

    def close(self) -> None:
        self.close_count += 1


class _FakeLogger:
    def __init__(self):
        self.success_calls = []
        self.failure_calls = []

    def dispatch_success_handlers(self, result, **kwargs):
        self.success_calls.append(result)

    def failure_handler(self, exception, traceback_exception, **kwargs):
        self.failure_calls.append(exception)


def _live_done_frame_with_usage() -> bytes:
    return (
        'data: {"delta":"hello ","type":"transcript.text.delta"}\n\n'
        'data: {"text":"hello world","usage":{"seconds":17.5770625},"type":"transcript.text.done"}\n\n'
    ).encode()


class TestDecodingAcrossNetworkBoundaries:
    @pytest.mark.parametrize("chunk_size", [1, 3, 7, 64, 1024])
    @pytest.mark.asyncio
    async def test_chunk_cuts_do_not_change_decoded_events(self, chunk_size):
        payload: bytes = _live_vocabulary_frames()
        transport = _FakeAsyncTransport(payload, chunk_size=chunk_size)
        stream = SharedNativeTranscriptionStream(model="m", response=transport, provider_config=_config())
        events = [event async for event in stream]
        assert [type(e) for e in events] == [
            NativeTranscriptDelta,
            NativeTranscriptDelta,
            NativeTranscriptDone,
        ]
        assert events[0].delta == "Four"
        assert events[2].text == "Four score and seven"
        assert events[2].usage["seconds"] == 17.5


class TestExactlyOnceTerminal:
    @pytest.mark.asyncio
    async def test_success_logging_carries_text_and_duration_facts(self):
        """The spend row is built from the logged summary: its text is the
        provider's terminal transcript and its hidden params carry the audio
        duration from the native usage, so cost logging survives the stream
        lifecycle instead of silently recording an empty transcript."""
        logger = _FakeLogger()
        stream = SharedNativeTranscriptionStream(
            model="m",
            response=_FakeAsyncTransport(_live_done_frame_with_usage()),
            provider_config=_config(),
            logging_obj=logger,
            hidden_params={"model_id": "dep-1"},
        )
        events = [event async for event in stream]
        assert isinstance(events[-1], NativeTranscriptDone)
        assert len(logger.success_calls) == 1
        summary = logger.success_calls[0]
        assert summary.text == "hello world"
        assert summary._hidden_params["audio_transcription_duration"] == 17.5770625
        assert summary._hidden_params["model_id"] == "dep-1"

    @pytest.mark.asyncio
    async def test_terminal_dispatch_builds_the_standard_logging_payload(self):
        """The stream's terminal success dispatch is the request's one and only
        spend-log event: it must run with final-response semantics so the
        generic handler builds the standard logging payload cost tracking and
        router accounting read (a streaming-flagged call defers the payload to
        the chunk-assembly path, which never runs for a native transcript)."""
        from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj

        logging_obj = LiteLLMLoggingObj(
            model="nvidia_nim/m",
            messages=[{"role": "user", "content": "transcribe this"}],
            stream=True,
            call_type="atranscription",
            start_time=None,
            litellm_call_id="stream-log-test",
            function_id="",
        )
        stream = SharedNativeTranscriptionStream(
            model="m",
            response=_FakeAsyncTransport(_live_done_frame_with_usage()),
            provider_config=_config(),
            logging_obj=logging_obj,
            hidden_params={"model_id": "dep-1"},
        )
        events = [event async for event in stream]
        assert isinstance(events[-1], NativeTranscriptDone)
        await asyncio.sleep(0.1)
        payload = logging_obj.model_call_details.get("standard_logging_object")
        assert payload is not None
        assert payload["id"] == "stream-log-test"
        assert logging_obj.stream is False

    @pytest.mark.asyncio
    async def test_a_terminal_after_done_never_dispatches_twice(self):
        """The first terminal event ends iteration exactly once; a stray
        second terminal in the same transport window can never dispatch a
        second success or failure log."""
        logger = _FakeLogger()
        payload = (
            'data: {"delta":"a","type":"transcript.text.delta"}\n\n'
            'data: {"text":"done once","type":"transcript.text.done"}\n\n'
            'data: {"message":"late failure","type":"error"}\n\n'
        ).encode()
        stream = SharedNativeTranscriptionStream(
            model="m",
            response=_FakeAsyncTransport(payload),
            provider_config=_config(),
            logging_obj=logger,
        )
        events = [event async for event in stream]
        # The done event ends the stream; the late error never surfaces.
        assert isinstance(events[-1], NativeTranscriptDone)
        assert len(logger.success_calls) == 1
        assert logger.failure_calls == []

    @pytest.mark.asyncio
    async def test_stream_end_without_terminal_yields_structured_error(self):
        payload = b'data: {"delta":"partial","type":"transcript.text.delta"}\n\n'
        stream = SharedNativeTranscriptionStream(
            model="m", response=_FakeAsyncTransport(payload), provider_config=_config()
        )
        events = [event async for event in stream]
        assert isinstance(events[-1], NativeTranscriptStreamError)
        assert "without a terminal event" in events[-1].message

    @pytest.mark.asyncio
    async def test_done_repeated_disposal_never_double_logs(self):
        logger = _FakeLogger()
        payload = _live_vocabulary_frames()
        stream = SharedNativeTranscriptionStream(
            model="m",
            response=_FakeAsyncTransport(payload),
            provider_config=_config(),
            logging_obj=logger,
        )
        events = [event async for event in stream]
        assert isinstance(events[-1], NativeTranscriptDone)
        await stream.aclose()
        await stream.aclose()
        await asyncio.sleep(0)
        assert len(logger.success_calls) == 1

    @pytest.mark.asyncio
    async def test_unrecognized_event_is_a_structured_error(self):
        payload = b'data: {"type":"vendor.some_future_kind","x":1}\n\n'
        stream = SharedNativeTranscriptionStream(
            model="m", response=_FakeAsyncTransport(payload), provider_config=_config()
        )
        events = [event async for event in stream]
        assert isinstance(events[0], NativeTranscriptStreamError)
        assert "unrecognized" in events[0].message


class TestCallerDisconnect:
    @pytest.mark.asyncio
    async def test_cancelled_iteration_closes_transport_without_terminal_logging(self):
        logger = _FakeLogger()
        payload = _live_vocabulary_frames()
        transport = _FakeAsyncTransport(payload, chunk_size=6, delay=0.05)
        stream = SharedNativeTranscriptionStream(
            model="m",
            response=transport,
            provider_config=_config(),
            logging_obj=logger,
        )
        generator = stream.__aiter__()
        first = await generator.__anext__()
        assert isinstance(first, NativeTranscriptDelta)
        await generator.aclose()
        assert transport.closed is True
        assert logger.success_calls == []
        assert logger.failure_calls == []

    @pytest.mark.asyncio
    async def test_midstream_exception_dispatches_failure_once_and_reraises(self):
        """An exception from the provider transport mid-stream surfaces to the
        caller (never a silent clean stream), dispatches terminal failure
        logging exactly once, and closes the upstream transport."""
        logger = _FakeLogger()

        class _ExplodingTransport:
            def __init__(self):
                self.closed = False

            async def aiter_bytes(self):
                yield b'data: {"delta":"par","type":"transcript.text.delta"}\n\n'
                raise RuntimeError("provider connection reset")

            async def aclose(self):
                self.closed = True

        transport = _ExplodingTransport()
        stream = SharedNativeTranscriptionStream(
            model="m",
            response=transport,
            provider_config=_config(),
            logging_obj=logger,
        )
        with pytest.raises(RuntimeError, match="provider connection reset"):
            async for event in stream:
                pass
        assert transport.closed is True
        assert len(logger.failure_calls) == 1
        assert logger.success_calls == []


class TestSyncFacade:
    def test_sync_events_match_async_contract(self):
        payload = _live_vocabulary_frames()
        stream = SyncNativeTranscriptionStream(
            model="m", response=_FakeSyncTransport(payload), provider_config=_config()
        )
        events = list(stream)
        assert [type(e) for e in events] == [
            NativeTranscriptDelta,
            NativeTranscriptDelta,
            NativeTranscriptDone,
        ]
        assert stream._response.close_count >= 1

    def test_sync_context_manager_closes_once(self):
        payload = _live_vocabulary_frames()
        transport = _FakeSyncTransport(payload)
        with SyncNativeTranscriptionStream(model="m", response=transport, provider_config=_config()) as stream:
            list(stream)
        assert transport.close_count == 1

    def test_sync_handler_string_stream_value_opens_the_stream(self):
        """The sync handler shares the multipart string contract: stream="true"
        opens the SSE stream, not a silent non-streaming call."""
        from litellm.llms.custom_httpx.http_handler import HTTPHandler
        from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_live_vocabulary_frames(),
            )

        handler = BaseLLMHTTPHandler()
        client = HTTPHandler(timeout=30, client=httpx.Client(transport=httpx.MockTransport(responder)))
        result = handler.audio_transcriptions(
            model="nvidia_nim/m",
            audio_file=io.BytesIO(b"fake audio"),
            optional_params={"stream": "true"},
            litellm_params={},
            model_response=None,
            timeout=30,
            max_retries=0,
            logging_obj=MagicMock(),
            api_key=None,
            api_base="http://nim",
            custom_llm_provider="nvidia_nim",
            client=client,
            headers={},
            provider_config=NvidiaNimAudioTranscriptionConfig(),
        )
        assert isinstance(result, SyncNativeTranscriptionStream)
        events = list(result)
        assert [type(e) for e in events] == [
            NativeTranscriptDelta,
            NativeTranscriptDelta,
            NativeTranscriptDone,
        ]


class TestProxySerialization:
    def test_delta_frame_keeps_native_event_vocabulary_and_fields(self):
        event = _config().transform_streaming_event("m", {"delta": "Four", "type": "transcript.text.delta"})
        frame = serialize_native_transcript_event(event)
        assert frame.startswith("event: transcript.text.delta\ndata: ")
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload["delta"] == "Four"
        assert payload["type"] == "transcript.text.delta"

    def test_done_frame_preserves_unknown_native_fields(self):
        event = _config().transform_streaming_event(
            "m",
            {
                "text": "t",
                "type": "transcript.text.done",
                "usage": {"seconds": 2},
                "vendor_future_field": {"nested": True},
            },
        )
        frame = serialize_native_transcript_event(event)
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload["vendor_future_field"] == {"nested": True}

    def test_error_frame_carries_message(self):
        frame = serialize_native_transcript_event(NativeTranscriptStreamError(message="boom"))
        assert frame.startswith("event: error\ndata: ")
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload["message"] == "boom"


class TestHandlerStreamingBranch:
    """The HTTP handler's stream-opening contract, through the real method."""

    @staticmethod
    def _handler() -> object:
        from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler

        return BaseLLMHTTPHandler()

    @staticmethod
    def _logging_obj():
        from unittest.mock import MagicMock

        return MagicMock()

    @pytest.mark.asyncio
    async def test_supported_provider_opens_a_stream_without_consuming_it(self):
        """stream=true on a streaming-capable config returns the initialized
        handle: no event has been read, and the handle exposes the hidden
        params the proxy needs to commit headers."""
        import httpx

        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
        from litellm.types.llms.audio_transcription import OpenedNativeAudioStream

        sse_body = (
            'event: transcript.text.delta\ndata: {"delta":"a","type":"transcript.text.delta"}\n\n'
            'event: transcript.text.done\ndata: {"text":"ab","type":"transcript.text.done"}\n\n'
        )

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=sse_body.encode(),
            )

        handler = self._handler()
        client = AsyncHTTPHandler(timeout=30, transport=httpx.MockTransport(responder))
        result = await handler.async_audio_transcriptions(
            model="nvidia_nim/m",
            audio_file=io.BytesIO(b"fake audio"),
            optional_params={"stream": True},
            litellm_params={},
            model_response=None,
            timeout=30,
            max_retries=0,
            logging_obj=self._logging_obj(),
            api_key=None,
            api_base="http://nim",
            custom_llm_provider="nvidia_nim",
            client=client,
            headers={},
            provider_config=NvidiaNimAudioTranscriptionConfig(),
        )
        assert isinstance(result, OpenedNativeAudioStream)
        assert result.model == "nvidia_nim/m"
        # The stream handle exists before iteration: the proxy can read
        # metadata and commit headers with the transport still open.
        assert "model" in result.hidden_params
        events = [event async for event in result.events]
        assert [type(e) for e in events] == [
            NativeTranscriptDelta,
            NativeTranscriptDone,
        ]
        assert events[1].text == "ab"

    @pytest.mark.asyncio
    async def test_multipart_string_stream_value_opens_the_stream(self):
        """The proxy route forwards multipart form fields verbatim, so
        stream=true arrives as the string "true": the handler must read the
        same client intent as the SDK's boolean True instead of silently
        dropping the field mid-flow (the provider would then answer SSE to a
        non-streaming call and the gateway would reject its own client)."""
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_live_vocabulary_frames(),
            )

        handler = self._handler()
        client = AsyncHTTPHandler(timeout=30, transport=httpx.MockTransport(responder))
        result = await handler.async_audio_transcriptions(
            model="nvidia_nim/m",
            audio_file=io.BytesIO(b"fake audio"),
            optional_params={"stream": "true"},
            litellm_params={},
            model_response=None,
            timeout=30,
            max_retries=0,
            logging_obj=self._logging_obj(),
            api_key=None,
            api_base="http://nim",
            custom_llm_provider="nvidia_nim",
            client=client,
            headers={},
            provider_config=NvidiaNimAudioTranscriptionConfig(),
        )
        assert isinstance(result, OpenedNativeAudioStream)
        events = [event async for event in result.events]
        assert [type(e) for e in events] == [
            NativeTranscriptDelta,
            NativeTranscriptDelta,
            NativeTranscriptDone,
        ]

    @pytest.mark.asyncio
    async def test_unsupported_provider_stream_is_a_truthful_400(self):
        """stream=true on a provider that did not opt into multipart streaming
        fails before any bytes are sent, instead of buffering or pretending."""
        import litellm

        handler = self._handler()
        with pytest.raises(litellm.BadRequestError, match="does not support streaming"):
            await handler.async_audio_transcriptions(
                model="hosted_vllm/m",
                audio_file=io.BytesIO(b"fake audio"),
                optional_params={"stream": True},
                litellm_params={},
                model_response=None,
                timeout=30,
                max_retries=0,
                logging_obj=self._logging_obj(),
                api_key=None,
                api_base="http://vllm",
                custom_llm_provider="hosted_vllm",
                client=None,
                headers={},
                provider_config=_HOSTED_VLLM_CONFIG(),
            )

    @pytest.mark.asyncio
    async def test_non_sse_answer_to_a_streaming_call_is_a_truthful_400(self):
        """A provider answering stream=true with JSON must not be relayed as a
        pretend event stream."""
        import httpx

        import litellm
        from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

        def responder(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"text": "hi"}',
            )

        handler = self._handler()
        client = AsyncHTTPHandler(timeout=30, transport=httpx.MockTransport(responder))
        with pytest.raises(litellm.BadRequestError, match="cannot relay"):
            await handler.async_audio_transcriptions(
                model="nvidia_nim/m",
                audio_file=io.BytesIO(b"fake audio"),
                optional_params={"stream": True},
                litellm_params={},
                model_response=None,
                timeout=30,
                max_retries=0,
                logging_obj=self._logging_obj(),
                api_key=None,
                api_base="http://nim",
                custom_llm_provider="nvidia_nim",
                client=client,
                headers={},
                provider_config=NvidiaNimAudioTranscriptionConfig(),
            )
