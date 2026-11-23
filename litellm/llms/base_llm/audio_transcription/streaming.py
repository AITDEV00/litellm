"""Generic lifecycle for native multipart transcript SSE streams.

The provider owns event vocabulary (its config's ``transform_streaming_event``
maps one decoded SSE JSON payload to one typed event); this module owns
everything else: SSE framing through the official OpenAI SDK ``SSEDecoder``,
the delta-then-terminal state machine, exactly-once terminal handling, and
idempotent upstream cancellation. It deliberately shares nothing with the
chat ``CustomStreamWrapper``, which carries chat-specific ``.sse``
expectations and token-based pricing.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import datetime
from typing import Final, Optional, Protocol

from openai._streaming import SSEDecoder

from litellm._logging import verbose_logger
from litellm.constants import LITELLM_MAX_STREAMING_DURATION_SECONDS
from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.types.llms.audio_transcription import (
    NativeTranscriptDelta,
    NativeTranscriptDone,
    NativeTranscriptEvent,
    NativeTranscriptStreamError,
)
from litellm.types.utils import TranscriptionResponse

_SSE_DONE_MARKER: Final = "[DONE]"


class _BytesResponseLike(Protocol):
    """The transport slice the stream needs from an opened httpx response."""

    def aiter_bytes(self) -> AsyncIterator[bytes]: ...

    async def aclose(self) -> None: ...


class SyncBytesResponseLike(Protocol):
    """The synchronous transport slice for ``SyncNativeTranscriptionStream``."""

    def iter_bytes(self) -> "Iterator[bytes]": ...

    def close(self) -> None: ...


class _StreamingAudioTranscriptionConfigLike(Protocol):
    """The provider-config slice the stream calls per decoded frame."""

    def transform_streaming_event(
        self, model: str, parsed_json: Mapping[str, object]
    ) -> Optional[NativeTranscriptEvent]: ...


class SharedNativeTranscriptionStream:
    """One opened native transcript stream with exactly-once finalization.

    ``response`` is the opened upstream transport (an httpx ``Response``
    sent with ``stream=True``, or any test double with the same
    ``aiter_bytes``/``aclose`` surface); the first event is consumed only
    when iteration starts, never at construction, so the proxy can commit
    headers from ``hidden_params`` before the first byte reaches the client.

    Terminal state is exactly-once: the provider must send one terminal
    event (done or error); a second one is itself an error. ``aclose`` and
    ``__aexit__`` are idempotent, and success/failure logging is dispatched
    from a single guarded transition so repeated disposal never logs twice.
    """

    def __init__(
        self,
        *,
        model: str,
        response: _BytesResponseLike,
        provider_config: _StreamingAudioTranscriptionConfigLike,
        logging_obj: Optional[LiteLLMLoggingObj] = None,
        hidden_params: Optional[dict[str, object]] = None,
        max_streaming_duration: float = LITELLM_MAX_STREAMING_DURATION_SECONDS,
    ):
        self.model = model
        self._response = response
        self._provider_config = provider_config
        self._logging_obj = logging_obj
        self.hidden_params: dict[str, object] = dict(hidden_params or {})
        self._max_streaming_duration = (
            max_streaming_duration if max_streaming_duration is not None else LITELLM_MAX_STREAMING_DURATION_SECONDS
        )
        self._sse = SSEDecoder()
        self._start_time: Final[datetime] = datetime.now()
        self._start_monotonic: Final[float] = time.monotonic()
        self._terminal_fired = False
        self._closed = False

    @property
    def meta(self) -> dict[str, object]:  # mutable-ok: same surface the proxy reads on results
        return self.hidden_params

    def __aiter__(self) -> AsyncIterator[NativeTranscriptEvent]:
        return self._events()

    async def __aenter__(self) -> SharedNativeTranscriptionStream:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Cancel the upstream transport. Idempotent; never double-logs."""
        if self._closed:
            return
        self._closed = True
        await self._close_upstream()

    async def _events(self) -> AsyncIterator[NativeTranscriptEvent]:
        try:
            # The decoder owns cross-chunk framing state, so the whole byte
            # iterator is handed to it once (per-chunk calls would re-seat its
            # internal buffer and split frames at network boundaries).
            async for sse in self._sse.aiter_bytes(self._response.aiter_bytes()):
                self._check_duration()
                event: Final = self._decode_event(sse)
                if event is None:
                    continue
                if isinstance(event, NativeTranscriptDelta):
                    yield event
                    continue
                self._enter_terminal(event)
                yield event
                return
            if not self._terminal_fired:
                # The upstream closed without a terminal event: a truncated
                # transcript must surface as a structured failure, never as a
                # clean-but-incomplete 200 stream.
                missing: Final = NativeTranscriptStreamError(
                    message="provider closed the transcript stream without a terminal event"
                )
                self._enter_terminal(missing)
                yield missing
        except (asyncio.CancelledError, GeneratorExit):
            # Caller disconnect: close the upstream transport, then re-raise
            # so the ASGI server also observes the cancellation. No terminal
            # logging: the client aborted before the provider answered.
            raise
        except Exception as e:
            if not self._terminal_fired:
                self._enter_terminal(NativeTranscriptStreamError(message=str(e)))
            raise
        finally:
            await self._close_upstream()

    def _decode_event(self, sse: object) -> Optional[NativeTranscriptEvent]:
        payload = getattr(sse, "data", None)
        if payload is None:
            return None
        if payload.strip() == _SSE_DONE_MARKER:
            return None
        try:
            parsed = json.loads(payload)
        except ValueError:
            return NativeTranscriptStreamError(message=f"provider sent a non-JSON SSE data payload: {payload[:120]!r}")
        if not isinstance(parsed, dict):
            return NativeTranscriptStreamError(
                message=f"provider sent an SSE data payload that is not a JSON object: {payload[:120]!r}"
            )
        event = self._provider_config.transform_streaming_event(self.model, parsed)
        if event is None:
            return NativeTranscriptStreamError(
                message=f"provider sent an unrecognized transcript event: {payload[:120]!r}"
            )
        return event

    def _check_duration(self) -> None:
        if self._max_streaming_duration is None or self._max_streaming_duration <= 0:
            return
        elapsed: Final = time.monotonic() - self._start_monotonic
        if elapsed > self._max_streaming_duration:
            raise RuntimeError(
                f"Transcription stream exceeded LITELLM_MAX_STREAMING_DURATION_SECONDS={self._max_streaming_duration}"
            )

    def _enter_terminal(self, event: NativeTranscriptEvent) -> None:
        """The single guarded terminal transition; logs exactly once."""
        if self._terminal_fired:
            raise RuntimeError("provider sent a second terminal transcript event")
        self._terminal_fired = True
        if isinstance(event, NativeTranscriptDone):
            self._log_success(event)
        else:
            self._log_failure(event)

    def _log_success(self, done: NativeTranscriptDone) -> None:
        if self._logging_obj is None:
            return
        duration: Final = self._extract_duration(done)
        if duration is not None:
            self.hidden_params["audio_transcription_duration"] = duration
        summary: Final = TranscriptionResponse(text=done.text)
        summary._hidden_params = dict(self.hidden_params)  # rebind-ok: dynamic-attr model contract
        end_time: Final = datetime.now()
        try:
            # The wrapper marked this call as streaming, which makes the
            # generic success handler defer the standard logging payload to
            # the chunk-assembly path that never runs here: the terminal
            # event IS the final result, so the logging object is flipped
            # back to final-response semantics before the one dispatch.
            self._logging_obj.stream = False  # rebind-ok: terminal state transition
            asyncio.create_task(
                self._logging_obj.dispatch_success_handlers(
                    summary,
                    start_time=self._start_time,
                    end_time=end_time,
                    prefer_async_handlers=True,
                )
            )
        except Exception as e:
            verbose_logger.debug("error dispatching native stream success logging: %s", e)

    def _log_failure(self, error: NativeTranscriptStreamError) -> None:
        if self._logging_obj is None:
            return
        exception: Final = RuntimeError(error.message)
        end_time: Final = datetime.now()
        try:
            self._logging_obj.failure_handler(
                exception=exception,
                traceback_exception=repr(exception),
                start_time=self._start_time,
                end_time=end_time,
            )
        except Exception as e:
            verbose_logger.debug("error dispatching native stream failure logging: %s", e)

    def _extract_duration(self, done: NativeTranscriptDone) -> float | None:
        usage = done.usage
        if isinstance(usage, Mapping):
            seconds = usage.get("seconds")
            if isinstance(seconds, (int, float)):
                return float(seconds)
        return None

    async def _close_upstream(self) -> None:
        try:
            await self._response.aclose()
        except Exception as e:
            verbose_logger.debug("error closing native transcript stream for %s: %s", self.model, e)


class SyncNativeTranscriptionStream:
    """The synchronous facade over one opened native transcript stream.

    The transport is opened synchronously (blocking connect, no event loop
    involvement) and events are decoded with the same ``SSEDecoder`` and the
    same provider vocabulary mapping as the async stream, so sync and async
    SDK callers observe an identical event contract. Exactly-once terminal
    semantics mirror ``SharedNativeTranscriptionStream``.
    """

    def __init__(
        self,
        *,
        model: str,
        response: "SyncBytesResponseLike",
        provider_config: _StreamingAudioTranscriptionConfigLike,
        hidden_params: Optional[dict[str, object]] = None,
        max_streaming_duration: float = LITELLM_MAX_STREAMING_DURATION_SECONDS,
    ):
        self.model = model
        self._response = response
        self._provider_config = provider_config
        self.hidden_params: dict[str, object] = dict(hidden_params or {})
        self._max_streaming_duration = (
            max_streaming_duration if max_streaming_duration is not None else LITELLM_MAX_STREAMING_DURATION_SECONDS
        )
        self._sse = SSEDecoder()
        self._start_monotonic: Final = time.monotonic()
        self._terminal_fired = False
        self._closed = False

    @property
    def meta(self) -> dict[str, object]:  # mutable-ok: same surface the proxy reads on results
        return self.hidden_params

    def __iter__(self):
        return self._events()

    def __enter__(self) -> SyncNativeTranscriptionStream:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        """Close the upstream transport. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        except Exception as e:
            verbose_logger.debug("error closing sync native transcript stream for %s: %s", self.model, e)

    def _events(self):
        try:
            for sse in self._sse.iter_bytes(self._response.iter_bytes()):
                self._check_duration()
                event = self._decode_event(sse)
                if event is None:
                    continue
                if isinstance(event, NativeTranscriptDelta):
                    yield event
                    continue
                if self._terminal_fired:
                    raise RuntimeError("provider sent a second terminal transcript event")
                self._terminal_fired = True
                yield event
                return
            if not self._terminal_fired:
                missing: Final = NativeTranscriptStreamError(
                    message="provider closed the transcript stream without a terminal event"
                )
                self._terminal_fired = True
                yield missing
        finally:
            self.close()

    def _decode_event(self, sse: object) -> Optional[NativeTranscriptEvent]:
        payload = getattr(sse, "data", None)
        if payload is None:
            return None
        if payload.strip() == _SSE_DONE_MARKER:
            return None
        try:
            parsed = json.loads(payload)
        except ValueError:
            return NativeTranscriptStreamError(message=f"provider sent a non-JSON SSE data payload: {payload[:120]!r}")
        if not isinstance(parsed, dict):
            return NativeTranscriptStreamError(
                message=f"provider sent an SSE data payload that is not a JSON object: {payload[:120]!r}"
            )
        event = self._provider_config.transform_streaming_event(self.model, parsed)
        if event is None:
            return NativeTranscriptStreamError(
                message=f"provider sent an unrecognized transcript event: {payload[:120]!r}"
            )
        return event

    def _check_duration(self) -> None:
        if self._max_streaming_duration is None or self._max_streaming_duration <= 0:
            return
        elapsed: Final = time.monotonic() - self._start_monotonic
        if elapsed > self._max_streaming_duration:
            raise RuntimeError(
                f"Transcription stream exceeded LITELLM_MAX_STREAMING_DURATION_SECONDS={self._max_streaming_duration}"
            )
