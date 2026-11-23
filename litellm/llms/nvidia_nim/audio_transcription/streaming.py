"""NVIDIA NIM native transcript event decoding.

Owns exactly the NIM event vocabulary, per the plan's split: the generic
stream lifecycle lives in ``base_llm.audio_transcription.streaming`` and this
module maps each decoded SSE ``data:`` JSON payload to one typed
``NativeTranscriptEvent``.

Verified against the live NIM transcribe service (2026-11-23, pod
``s-3fb782ae-...adeo.svc.cluster.local:8080``):

- ``transcript.text.delta`` frames carry ``{"delta": "...", "type": ...}``
- one terminal ``transcript.text.done`` frame carries
  ``{"text": ..., "transcription": {...}, "usage": {...}, "type": ...}``
  where ``usage.seconds`` is the audio duration in seconds
- the service ends the stream by closing the connection after the done
  event; no ``[DONE]`` marker follows
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Optional

from litellm.types.llms.audio_transcription import (
    NativeTranscriptDelta,
    NativeTranscriptDone,
    NativeTranscriptEvent,
    NativeTranscriptStreamError,
)

_DELTA_EVENT_TYPE: Final = "transcript.text.delta"
_DONE_EVENT_TYPE: Final = "transcript.text.done"
_ERROR_EVENT_TYPE: Final = "error"


def transform_streaming_audio_response(
    model: str, parsed_json: Mapping[str, object]
) -> Optional[NativeTranscriptEvent]:
    """Map one decoded NIM SSE JSON payload to one typed transcript event.

    Unknown keys survive on ``native_event`` so vendor metadata is never
    dropped. An event type outside the verified vocabulary is an explicit
    error event (the client already has headers and prior deltas, so the
    failure must be structured, not silent).
    """
    event_type = parsed_json.get("type")
    if event_type == _DELTA_EVENT_TYPE:
        delta = parsed_json.get("delta")
        if not isinstance(delta, str):
            return NativeTranscriptStreamError(
                message="NIM transcript.text.delta carried no string delta field"
            )
        return NativeTranscriptDelta(delta=delta, native_event=dict(parsed_json))
    if event_type == _DONE_EVENT_TYPE:
        text = parsed_json.get("text")
        if not isinstance(text, str):
            return NativeTranscriptStreamError(
                message="NIM transcript.text.done carried no string text field"
            )
        usage = parsed_json.get("usage")
        return NativeTranscriptDone(
            text=text,
            usage=dict(usage) if isinstance(usage, Mapping) else {},
            native_event=dict(parsed_json),
        )
    if event_type == _ERROR_EVENT_TYPE:
        message = parsed_json.get("message")
        return NativeTranscriptStreamError(
            message=message if isinstance(message, str) else "NIM sent an error event",
            native_event=dict(parsed_json),
        )
    return NativeTranscriptStreamError(
        message=f"NIM sent an unrecognized transcript event type: {event_type!r}",
        native_event=dict(parsed_json),
    )
