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
from typing import Final, Protocol

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
    ) -> NativeTranscriptEvent | None: ...


class SharedNativeTranscriptionStream:
    """One opened native transcript stream with exactly-once finalization.

    ``response`` is the opened upstream transport (an httpx ``Response``
    sent with ``stream=True``, or any test double with the same
    ``aiter_bytes``/``aclose`` surface); the first event is consumed only
    when iteration starts, never at construction, so the proxy can commit
    headers from ``hidden_params`` before the first byte reaches the client.

    Terminal state is exactly-once: after the first done or error, iteration
    ends immediately and trailing frames are ignored. ``aclose`` and
    ``__aexit__`` are idempotent, and success/failure logging is dispatched
    from a single guarded transition so repeated disposal never logs twice.
    """

    def __init__(
        self,
        *,
        model: str,
        response: _BytesResponseLike,
        provider_config: _StreamingAudioTranscriptionConfigLike,
        logging_obj: LiteLLMLoggingObj | None = None,
        hidden_params: dict[str, object] | None = None,
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
        self._terminal_tasks: set[asyncio.Task[object]] = (
            set()
        )  # mutable-ok: lifecycle bookkeeping, never public event state

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
        """Close transport and await any terminal callbacks already created."""
        if not self._closed:
            self._closed = True
            await self._close_upstream()
        if self._terminal_tasks:
            await self._finish_logging()

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
        except Exception as e:  # noqa: BLE001  # transport/decode failures become typed audio stream errors
            if not self._terminal_fired:
                error: Final = NativeTranscriptStreamError(message=str(e))
                self._enter_terminal(error)
                yield error
        finally:
            await self._close_upstream()
            await self._finish_logging()

    async def _finish_logging(self) -> None:
        results: Final = await asyncio.gather(*tuple(self._terminal_tasks), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                verbose_logger.debug("native transcript terminal callback failed: %s", result)

    def _decode_event(self, sse: object) -> NativeTranscriptEvent | None:
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

    def _summary(self, done: NativeTranscriptDone) -> TranscriptionResponse:
        duration: Final = self._extract_duration(done)
        hidden_params: Final = {
            **self.hidden_params,
            **({"audio_transcription_duration": duration} if duration is not None else {}),
        }
        summary: Final = TranscriptionResponse(text=done.text)
        summary._hidden_params = hidden_params  # rebind-ok: dynamic-attr model contract
        for key, event in done.native_event.items():
            if key not in ("type", "text"):
                setattr(summary, key, event)
        return summary

    def _log_success(self, done: NativeTranscriptDone) -> None:
        logger: Final = self._logging_obj
        if logger is None:
            return
        summary: Final = self._summary(done)
        end_time: Final = datetime.now()  # noqa: DTZ005  # LiteLLM logging uses naive timestamps
        try:
            self.hidden_params.update(summary.hidden_params)
            logger.stream = False  # rebind-ok: native transcript terminal event is a final audio response
            terminal: Final = logger.dispatch_success_handlers(
                summary,
                start_time=self._start_time,
                end_time=end_time,
                prefer_async_handlers=True,
            )
            if hasattr(terminal, "__await__"):
                self._terminal_tasks.add(asyncio.create_task(terminal))
        except Exception as e:  # noqa: BLE001  # callback failures cannot corrupt transcript delivery
            verbose_logger.debug("error dispatching native stream success logging: %s", e)

    def _log_failure(self, error: NativeTranscriptStreamError) -> None:
        logger: Final = self._logging_obj
        if logger is None:
            return
        exception: Final = RuntimeError(error.message)
        try:
            logger.stream = False  # rebind-ok: terminal event carries final audio outcome, not chat chunks
            terminal: Final = logger.dispatch_failure_handlers(
                exception=exception,
                traceback_exception=repr(exception),
                prefer_async_handlers=True,
            )
            if hasattr(terminal, "__await__"):
                self._terminal_tasks.add(asyncio.create_task(terminal))
        except Exception as e:  # noqa: BLE001  # callback failures cannot corrupt transcript delivery
            verbose_logger.debug("error dispatching native stream failure logging: %s", e)

    def log_final_sync(self, event: NativeTranscriptEvent) -> None:
        logger: Final = self._logging_obj
        if logger is None:
            return
        try:
            logger.stream = False  # rebind-ok: native transcript completion, not one chat token
            if isinstance(event, NativeTranscriptDone):
                logger.success_handler(
                    self._summary(event),
                    start_time=self._start_time,
                    end_time=datetime.now(),  # noqa: DTZ005  # LiteLLM logging uses naive timestamps
                )
                return
            logger.failure_handler(
                exception=RuntimeError(event.message),
                traceback_exception=repr(RuntimeError(event.message)),
                start_time=self._start_time,
                end_time=datetime.now(),  # noqa: DTZ005  # LiteLLM logging uses naive timestamps
            )
        except Exception as e:
            verbose_logger.debug("error dispatching sync native stream terminal logging: %s", e)

    @staticmethod
    def _extract_duration(done: NativeTranscriptDone) -> float | None:
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
        logging_obj: LiteLLMLoggingObj | None = None,
        hidden_params: dict[str, object] | None = None,
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
        self._start_monotonic: Final = time.monotonic()
        self._terminal_fired = False
        self._closed = False
        self._start_time: Final[datetime] = datetime.now()  # noqa: DTZ005  # naive framework logging clock
        self._terminal_logging = SharedNativeTranscriptionStream(
            model=model,
            response=response,
            provider_config=provider_config,
            logging_obj=logging_obj,
            hidden_params=self.hidden_params,
        ).log_final_sync

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
                self._terminal_logging(event)
                yield event
                return
            if not self._terminal_fired:
                missing: Final = NativeTranscriptStreamError(
                    message="provider closed the transcript stream without a terminal event"
                )
                self._terminal_fired = True
                self._terminal_logging(missing)
                yield missing
        except GeneratorExit:
            raise
        except Exception as e:  # noqa: BLE001  # transport failures surface as typed audio stream errors
            if not self._terminal_fired:
                error: Final = NativeTranscriptStreamError(message=str(e))
                self._terminal_fired = True
                self._terminal_logging(error)
                yield error
        finally:
            self.close()

    def _decode_event(self, sse: object) -> NativeTranscriptEvent | None:
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
