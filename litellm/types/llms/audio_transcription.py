"""Typed result envelopes for native multipart audio operations.

Providers opting into native multipart transport (hosted-vLLM style servers,
NVIDIA NIM transcribe) can answer with more than transcription JSON: a plain
text transcript, an SRT document, or a VTT document. Those responses cannot be
forced through the ``TranscriptionResponse`` pydantic model without either
wrapping them in a fake JSON envelope or corrupting the delivered bytes.

The types here are the public contract between the provider HTTP handler, the
SDK entry points, and the proxy route return. JSON results stay
``TranscriptionResponse``; document results become
``NativeAudioDocumentResponse`` and flow out to a raw ``Response``.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import TypeAlias

from litellm.types.utils import TranscriptionResponse

NativeAudioJsonResult: TypeAlias = TranscriptionResponse


@dataclass(frozen=True, slots=True)
class NativeTranscriptDelta:
    """One incremental transcript fragment, in arrival order.

    ``native_event`` preserves the provider's own event fields beyond the
    mapped ones so downstream consumers can inspect vendor metadata without
    the gateway hardcoding a per-field list.
    """

    delta: str
    native_event: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NativeTranscriptDone:
    """The single terminal success event of a native transcript stream.

    ``text`` is the complete final transcript. ``usage`` carries the
    provider's native usage payload (NIM: audio/completion/prompt token
    counts plus ``seconds``), preserved as facts rather than re-interpreted
    as chat tokens. ``native_event`` keeps the full provider frame.
    """

    text: str
    usage: Mapping[str, object] = field(default_factory=dict)
    native_event: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NativeTranscriptStreamError:
    """A structured mid-stream failure: the client already has headers and
    prior deltas, so the stream emits this typed event once, then ends."""

    message: str
    native_event: Mapping[str, object] = field(default_factory=dict)


NativeTranscriptEvent: TypeAlias = (
    NativeTranscriptDelta | NativeTranscriptDone | NativeTranscriptStreamError
)


@dataclass(frozen=True, slots=True)
class NativeAudioDocumentResponse:
    """A non-JSON transcription/translation body delivered as document bytes.

    ``body`` is the decoded document text (per ``charset`` or UTF-8), never a
    JSON wrapper, so ``response_format=text`` returns the bare transcript and
    ``response_format=srt`` returns the subtitle document. ``media_type`` is
    the content type the provider declared and the gateway must re-emit.

    ``_hidden_params`` carries the same internal metadata a
    ``TranscriptionResponse`` would (model, model_id, response_cost, cache
    facts), so logging, cost and header building read one shape for every
    response medium. It is excluded from the public body.
    """

    body: str
    media_type: str
    charset: str | None = None
    provider_response_headers: Mapping[str, str] = field(default_factory=dict)
    _hidden_params: dict[str, object] = field(default_factory=dict)

    @property
    def hidden_params(self) -> dict[str, object]:  # mutable-ok: API requires mutation
        """Same surface ``TranscriptionResponse`` exposes, so header/cost
        helpers treat both response media identically."""
        return self._hidden_params


NonStreamingNativeAudioResult: TypeAlias = TranscriptionResponse | NativeAudioDocumentResponse


@dataclass(frozen=True, slots=True)
class OpenedNativeAudioStream:
    """An initialized native transcript SSE stream, before first iteration.

    The HTTP layer returns this instead of a final result when the provider
    supports multipart streaming and the caller requested one. Headers and
    status were already validated against the provider's contract, so the
    proxy can inspect ``hidden_params`` and commit headers before consuming
    a single event; the transport stays open until the consumer iterates
    ``events`` to completion or calls ``aclose``.

    Mirrors the ``HttpxBinaryResponseContent`` precedent (a closeable
    streaming response owned by a first-class route), except it yields typed
    transcript events rather than audio chunks.
    """

    model: str
    events: AsyncIterator[NativeTranscriptEvent]
    hidden_params: dict[str, object] = field(default_factory=dict)
    aclose: object = field(default=None)

    @property
    def meta(self) -> dict[str, object]:  # mutable-ok: same surface as TranscriptionResponse
        """Alias for the hidden-params surface the proxy already reads."""
        return self.hidden_params


NativeAudioStreamResult: TypeAlias = OpenedNativeAudioStream | TranscriptionResponse | NativeAudioDocumentResponse
