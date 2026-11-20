"""Shared request/response transformation for native multipart audio ops.

One implementation of the OpenAI-compatible multipart contract, composed
(never inherited across serving identities) by the providers whose serving
stack actually accepts it: hosted-vLLM style servers and the NVIDIA NIM
transcribe service. Each provider config keeps its own identity: auth, api
base, URL path, and error class stay in the provider module; only the wire
shape lives here.
"""

from __future__ import annotations

from typing import Final

import httpx

from litellm.litellm_core_utils.audio_utils.utils import process_audio_file
from litellm.litellm_core_utils.llm_request_utils import native_multipart_fields
from litellm.litellm_core_utils.llm_response_utils.transcriptions import (
    hydrate_transcription_response,
)
from litellm.llms.base_llm.audio_transcription.transformation import (
    AudioTranscriptionRequestData,
)
from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse
from litellm.types.utils import FileTypes, TranscriptionResponse

_JSON_CONTENT_TYPE: Final = "application/json"
_DOCUMENT_MEDIA_TYPES: Final = frozenset(
    {
        "text/plain",
        "application/x-subrip",
        "text/vtt",
    }
)


def _charset_from_content_type(content_type: str) -> str | None:
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip().strip('"')
    return None


class OpenAICompatibleNativeAudioMultipartTransformation:
    """Composable wire-shape helper for native multipart transcription requests.

    Providers compose this instead of inheriting another provider's config
    class: the provider config supplies its URL path, auth, and the standard
    OpenAI parameters it supports; this class encodes the request body and
    parses the response body once, identically for every consumer.
    """

    def build_native_multipart_request(
        self,
        *,
        model: str,
        audio_file: FileTypes,
        optional_params: dict,
        supported_openai_params: tuple[str, ...] | list[str],
    ) -> AudioTranscriptionRequestData:
        """Encode the multipart request: ordered scalar form fields plus the upload.

        The form fields are the typed native contract: standard OpenAI
        parameters the provider supports, then every native extension param
        (``diarization``, ``word_timestamps``, ``target_language``, ...), then
        ``extra_body`` keys unpacked top-level. Lists repeat the field per
        element so ``timestamp_granularities=["word", "segment"]`` reaches the
        server as two parts.
        """
        processed_audio: Final = process_audio_file(audio_file)
        standard_fields: Final = {
            key: optional_params[key]
            for key in supported_openai_params
            if optional_params.get(key) is not None
        }
        extension_fields: Final = {
            key: value
            for key, value in optional_params.items()
            if key not in standard_fields and key != "model" and key != "extra_body"
        }
        extra_body_fields: Final = optional_params.get("extra_body")

        fields: Final = native_multipart_fields(
            {"model": model},
            standard_fields,
            extension_fields,
            extra_body=extra_body_fields if isinstance(extra_body_fields, dict) else None,
        )

        # httpx multipart requires a Mapping: ``data.items()`` with scalar
        # values, and a list value repeats the field per element. Collapsing
        # the ordered pairs into an insertion-ordered dict of scalars/lists
        # preserves both cross-key order and repeated-part order.
        data: Final[dict[str, object]] = {}
        for name, value in fields:
            existing: Final = data.get(name)
            if isinstance(existing, list):
                existing.append(value)
            elif existing is not None:
                data[name] = [existing, value]
            else:
                data[name] = value

        files: Final = {
            "file": (
                processed_audio.filename,
                processed_audio.file_content,
                processed_audio.content_type,
            )
        }

        return AudioTranscriptionRequestData(
            data=data,
            files=files,
        )

    def parse_native_audio_response(
        self,
        raw_response: httpx.Response,
    ) -> TranscriptionResponse | NativeAudioDocumentResponse:
        """Content-aware response parsing.

        A JSON body (including ``verbose_json`` with unrecognized native
        fields) hydrates a ``TranscriptionResponse`` with every public field
        preserved. A text/SRT/VTT body becomes a ``NativeAudioDocumentResponse``
        carrying the raw decoded body and the provider's declared media type,
        never a fake JSON envelope. HTTP error statuses are the caller's
        concern: this method runs only on 2xx.
        """
        content_type: Final = _header_value(raw_response.headers, "content-type", "").split(";")[0].strip().lower()
        if content_type and content_type not in _DOCUMENT_MEDIA_TYPES:
            payload: Final = raw_response.json()
            return hydrate_transcription_response(response_object=payload)

        media_type: Final = content_type or "text/plain"
        charset: Final = _charset_from_content_type(_header_value(raw_response.headers, "content-type", ""))
        body: Final = raw_response.text if charset is None else raw_response.content.decode(charset, errors="replace")
        return NativeAudioDocumentResponse(
            body=body,
            media_type=media_type,
            charset=charset,
            provider_response_headers={str(k): v for k, v in raw_response.headers.items()},
        )


def _header_value(headers: httpx.Headers, key: str, default: str) -> str:
    return headers.get(key, default)
