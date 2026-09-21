"""
Per-request stream lifecycle tracer.

Enabled via `LITELLM_STREAM_TRACE_PATH` (opt-in; off when unset). Writes
JSON Lines — one record per event — to a per-pod file. Route that file to
PVC/Loki/CloudWatch to answer:

  * Did the client disconnect mid-stream, or did the server stop sending?
  * TTFC (time to first chunk) per request.
  * Per-chunk gap times — the exact distribution before a timeout fired.
  * Terminal state: success / client_abort / upstream_timeout / provider_error.

The hot path is a single dataclass + one json.dumps + one file.write per
chunk — no awaits, no locks, no metrics stubs. The file is opened once per
process and flushed via line-buffered IO so it survives SIGKILL minus the
last ~4KiB.
"""

from __future__ import annotations

import atexit
import datetime
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import IO, Final, Literal

StreamEndKind = Literal["success", "client_abort", "upstream_timeout", "provider_error", "stream_error", "unknown"]

_ENABLED: Final = bool(os.getenv("LITELLM_STREAM_TRACE_PATH"))
_PATH: Final = os.getenv("LITELLM_STREAM_TRACE_PATH", "")
_RECORD_GAPS_EV: Final = int(os.getenv("LITELLM_STREAM_TRACE_GAP_THRESHOLD_MS", "0"))  # 0 = record every chunk

_lock: Final = threading.Lock()
_writer: IO[str] | None = None


def _open_writer() -> IO[str]:
    # append + line-buffered so a tail follows live
    return open(_PATH, "a", buffering=1, encoding="utf-8")


def _writer_or_raise() -> IO[str]:
    global _writer
    with _lock:
        if _writer is None:
            _writer = _open_writer()
        return _writer


def _close_writer() -> None:
    global _writer
    with _lock:
        if _writer is not None:
            try:
                _writer.flush()
                _writer.close()
            finally:
                _writer = None


if _ENABLED:
    atexit.register(_close_writer)


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """One lifecycle record."""

    event: str  # "start" | "first_chunk" | "chunk" | "end" | "error"
    request_id: str
    ts: str  # ISO 8601 UTC
    model: str
    provider: str
    api_base: str

    # populated depending on event kind
    elapsed_ms: float | None = None  # ms since stream start
    since_prev_chunk_ms: float | None = None
    chunk_seq: int | None = None
    chunk_chars: int | None = None
    finish_reason: str | None = None
    error_type: str | None = None
    error_msg: str | None = None
    end_kind: StreamEndKind | None = None
    gap_histogram_ms: tuple[int, ...] = ()  # recorded gaps > threshold, sorted


@dataclass(slots=True)
class _StreamTracer:
    """Per-request state. Lives on CustomStreamWrapper so it survives re-entry."""

    request_id: str
    model: str
    provider: str
    api_base: str
    started_monotonic: float
    started_iso: str
    chunk_seq: int = 0
    first_chunk_recorded: bool = False
    last_chunk_monotonic: float | None = None
    large_gaps: list[int] = field(default_factory=list)  # python list, converted at end

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self.started_monotonic) * 1000.0

    def record_chunk(self, *, chars: int, finish_reason: str | None = None) -> None:
        if not _ENABLED:
            return
        now = time.monotonic()
        since_prev: float | None = None
        if self.last_chunk_monotonic is not None:
            since_prev = (now - self.last_chunk_monotonic) * 1000.0
            if _RECORD_GAPS_EV and since_prev and since_prev >= _RECORD_GAPS_EV:
                self.large_gaps.append(int(since_prev))
        self.chunk_seq += 1
        kind = "first_chunk" if not self.first_chunk_recorded else "chunk"
        self.first_chunk_recorded = True
        self.last_chunk_monotonic = now

        event = StreamEvent(
            event=kind,
            request_id=self.request_id,
            ts=datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
            model=self.model,
            provider=self.provider,
            api_base=self.api_base,
            elapsed_ms=(now - self.started_monotonic) * 1000.0,
            since_prev_chunk_ms=since_prev,
            chunk_seq=self.chunk_seq,
            chunk_chars=chars,
            finish_reason=finish_reason,
        )
        self._emit(event)

    def record_end(self, kind: StreamEndKind, *, error_type: str | None = None, error_msg: str | None = None) -> None:
        if not _ENABLED:
            return
        event = StreamEvent(
            event="end" if kind == "success" else "error",
            request_id=self.request_id,
            ts=datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
            model=self.model,
            provider=self.provider,
            api_base=self.api_base,
            elapsed_ms=self.elapsed_ms(),
            chunk_seq=self.chunk_seq,
            end_kind=kind,
            error_type=error_type,
            error_msg=error_msg,
            gap_histogram_ms=tuple(sorted(self.large_gaps)),
        )
        self._emit(event)

    def _emit(self, event: StreamEvent) -> None:
        try:
            _writer_or_raise().write(json.dumps(asdict(event), default=str) + "\n")
        except OSError:
            pass  # disk full / rotated away — do not break streaming


def begin_trace(*, request_id: str, model: str, provider: str, api_base: str) -> "_StreamTracer | None":
    """Called when a CustomStreamWrapper is created. Returns None when disabled."""
    if not _ENABLED:
        return None
    started_monotonic = time.monotonic()
    tracer = _StreamTracer(
        request_id=request_id,
        model=model,
        provider=provider,
        api_base=api_base,
        started_monotonic=started_monotonic,
        started_iso=datetime.datetime.now(tz=datetime.timezone.utc).isoformat(),
    )
    event = StreamEvent(
        event="start",
        request_id=request_id,
        model=model,
        provider=provider,
        api_base=api_base,
        ts=tracer.started_iso,
        elapsed_ms=0.0,
    )
    tracer._emit(event)
    return tracer


def is_enabled() -> bool:
    return _ENABLED
