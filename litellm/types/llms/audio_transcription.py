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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypeAlias

from litellm.types.utils import TranscriptionResponse

NativeAudioJsonResult: TypeAlias = TranscriptionResponse


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
