"""
Tests for HostedVLLMAudioTranscriptionConfig request/response transformation.

The request transform now encodes through the shared native multipart
contract: ordered ``(name, value)`` form-field pairs (a list, not a dict, so a
repeated field can appear twice), the upload split into ``files``, and native
extension params preserved instead of being discarded by the standard-parameter
allowlist. Response parsing is content-aware: JSON hydrates a
``TranscriptionResponse`` with every public field preserved; a text/SRT/VTT
body becomes a ``NativeAudioDocumentResponse`` with the raw document bytes.
"""

import io
import json

import httpx
import pytest

import litellm
from litellm.llms.base_llm.audio_transcription.transformation import (
    AudioTranscriptionRequestData,
)
from litellm.llms.hosted_vllm.transcriptions.transformation import (
    HostedVLLMAudioTranscriptionConfig,
)


def _field_dict(result: AudioTranscriptionRequestData) -> dict[str, object]:
    """Last-wins view of the ordered form fields, for value assertions."""
    return dict(result.data)


def _field_names(result: AudioTranscriptionRequestData) -> list[str]:
    return list(result.data)


def _wire_parts(result: AudioTranscriptionRequestData) -> list[tuple[str, str]]:
    """Encode exactly as the http handler sends (httpx data= + files=) and
    parse the multipart wire body back into ordered (name, value) parts."""
    import httpx

    request = httpx.Request(
        "POST",
        "http://backend/v1/audio/transcriptions",
        data=result.data,
        files=[("file", result.files["file"])] if result.files else None,
    )
    request.read()
    body = request.content.decode("utf-8", "replace")
    boundary = body.split("\r\n")[0].strip()
    parts: list[tuple[str, str]] = []
    for section in body.split(boundary)[1:]:
        if 'name="' not in section:
            continue
        header_block, _, value = section.partition("\r\n\r\n")
        disposition = next(line for line in header_block.splitlines() if "form-data" in line)
        name = disposition.split('name="')[1].split('"')[0]
        if "filename=" in disposition:
            continue
        parts.append((name, value[: value.rfind("\r\n")]))
    return parts


class TestHostedVLLMAudioTranscriptionTransform:
    def _transform(self, audio_file, optional_params: dict) -> AudioTranscriptionRequestData:
        config = HostedVLLMAudioTranscriptionConfig()
        return config.transform_audio_transcription_request(
            model="Qwen/Qwen3-ASR-1.7B",
            audio_file=audio_file,
            optional_params=optional_params,
            litellm_params={},
        )

    def test_file_is_moved_to_files_dict_not_in_data(self):
        """The BytesIO file must land in ``files`` (multipart), never in ``data``.

        If the file stayed in ``data`` (as the inherited whisper transform does),
        ``base_llm_http_handler`` would JSON-serialize it and raise
        "Object of type BytesIO is not JSON serializable". This is the exact
        regression seen against a live hosted_vllm ASR endpoint.
        """
        result = self._transform(io.BytesIO(b"fake audio bytes"), {"temperature": 0.0})

        # data must contain no file object and be form fields only
        assert "file" not in _field_dict(result)
        assert _field_dict(result) == {"model": "Qwen/Qwen3-ASR-1.7B", "temperature": 0.0}

        # the file must be present as a multipart (filename, content, content_type) tuple
        assert result.files is not None
        assert "file" in result.files
        filename, content, content_type = result.files["file"]
        assert filename
        assert content == b"fake audio bytes"
        assert content_type

        # every form-field value is scalar-or-list-of-scalars, the shape the
        # httpx multipart encoder accepts
        for value in result.data.values():
            for entry in (value if isinstance(value, list) else (value,)):
                assert isinstance(entry, (str, int, float, bool))

    def test_passes_through_supported_openai_params(self):
        """Language/temperature/prompt/responses flow into the form fields."""
        result = self._transform(
            io.BytesIO(b"audio"),
            {
                "language": "en",
                "temperature": 0.0,
                "response_format": "json",
                "prompt": "transcribe carefully",
            },
        )
        fields = _field_dict(result)
        assert fields["language"] == "en"
        assert fields["temperature"] == 0.0
        assert fields["response_format"] == "json"
        assert fields["prompt"] == "transcribe carefully"

    def test_preserves_native_extension_params(self):
        """Vendor extension params are forwarded as top-level form fields.

        Native backends own their form schema, so ``diarization`` must reach the
        server rather than being dropped by the OpenAI allow-list (the plan's
        headline fix: extra fields were lost at this exact point).
        """
        result = self._transform(io.BytesIO(b"audio"), {"diarization": True, "word_timestamps": True})
        fields = _field_dict(result)
        assert fields["diarization"] is True
        assert fields["word_timestamps"] is True

    def test_repeated_field_keeps_both_values_in_order(self):
        """A list-valued param repeats the form field once per element.

        ``timestamp_granularities=["word", "segment"]`` must reach the server as
        two parts, not collapse to the last value under a dict update.
        """
        result = self._transform(io.BytesIO(b"audio"), {"timestamp_granularities": ["word", "segment"]})
        assert result.data["timestamp_granularities"] == ["word", "segment"]
        names = [name for name, _ in _wire_parts(result)]
        assert names.count("timestamp_granularities") == 2

    def test_extra_body_unpacks_to_top_level_fields(self):
        """``extra_body`` keys become top-level form fields, not a nested JSON part."""
        result = self._transform(io.BytesIO(b"audio"), {"extra_body": {"custom_k": "v"}})
        assert _field_dict(result).get("custom_k") == "v"
        assert "extra_body" not in result.data

    def test_drops_non_scalar_and_internal_fields(self):
        """Non-scalar values and internal routing keys never reach the form."""
        result = self._transform(io.BytesIO(b"audio"), {"nested": {"a": 1}, "keepalive_seconds": 3600})
        names = _field_names(result)
        assert "nested" not in names
        assert "keepalive_seconds" not in names

    def test_handles_filename_carrying_bytesio(self):
        """A BytesIO with a .name attribute uses that filename and a sensible content type."""
        buf = io.BytesIO(b"abc")
        buf.name = "clip.mp3"
        result = self._transform(buf, {})
        filename, content, content_type = result.files["file"]
        assert filename == "clip.mp3"
        assert content == b"abc"
        assert content_type


class TestHostedVLLMAudioTranscriptionResponse:
    def _response(self, payload: dict) -> httpx.Response:
        return httpx.Response(200, json=payload)

    def test_an_sse_answer_to_a_nonstreaming_call_is_a_truthful_error(self):
        """An upstream that answers a non-streaming request with SSE frames
        must surface a 400-class error, not a JSON parse crash: "data:" lines
        cannot be truthfully repackaged as a final response."""
        sse_response = httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"type": "transcript.text.delta", "delta": "Fo"}\n\n',
        )

        with pytest.raises(litellm.BadRequestError, match="streaming is not supported"):
            HostedVLLMAudioTranscriptionConfig().transform_audio_transcription_response(sse_response)

    def test_handles_usage_field(self):
        """A provider ``usage`` field must not crash response construction.

        The inherited whisper transform does ``TranscriptionResponse(**json)``,
        which fails with "unexpected keyword argument 'usage'" because the
        response model only accepts ``text``. Qwen ASR returns a ``usage``
        field, so this is the exact regression seen against a live hosted_vllm
        ASR endpoint.
        """
        result = HostedVLLMAudioTranscriptionConfig().transform_audio_transcription_response(
            self._response(
                {
                    "text": "hello world",
                    "usage": {"type": "tokens", "input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                }
            )
        )

        assert result.text == "hello world"
        assert result["usage"]["total_tokens"] == 8
        assert result["usage"] == {"type": "tokens", "input_tokens": 5, "output_tokens": 3, "total_tokens": 8}

    def test_keeps_other_provider_fields(self):
        """Extra non-text keys survive on the response for logging/audit."""
        result = HostedVLLMAudioTranscriptionConfig().transform_audio_transcription_response(
            self._response({"text": "hi", "task": "transcribe", "language": "en"})
        )

        assert result.text == "hi"
        assert result["task"] == "transcribe"
        assert result["language"] == "en"

    def test_empty_text(self):
        """A response with no text yields an empty string, not a crash."""
        result = HostedVLLMAudioTranscriptionConfig().transform_audio_transcription_response(
            self._response({"usage": {"type": "tokens", "total_tokens": 0}})
        )

        assert result.text == ""

    def test_preserves_rich_native_fields(self):
        """Every native public field survives response parsing.

        A cold response's rich optional fields (schema_version,
        diarization_timestamps, ...) must land on the response object so the
        shared serializer emits them; the cache hydrator preserves the same
        set, keeping cold and cached public JSON identical.
        """
        result = HostedVLLMAudioTranscriptionConfig().transform_audio_transcription_response(
            self._response(
                {
                    "text": "hello world",
                    "schema_version": "1.0",
                    "transcript_origin": "asmr",
                    "diarization_frame_seconds": 0.08,
                    "word_timestamp_alignment": True,
                    "diarization_timestamps": [{"speaker": 1, "start": 0.0, "end": 1.2}],
                }
            )
        )
        assert result.text == "hello world"
        assert result["schema_version"] == "1.0"
        assert result["transcript_origin"] == "asmr"
        assert result["diarization_frame_seconds"] == 0.08
        assert result["word_timestamp_alignment"] is True
        assert result["diarization_timestamps"] == [{"speaker": 1, "start": 0.0, "end": 1.2}]
        dumped = result.model_dump()
        for key in ("schema_version", "transcript_origin", "diarization_frame_seconds"):
            assert key in dumped, f"{key} must survive the public dump"

    def _document_response(self, body: str, media_type: str) -> httpx.Response:
        return httpx.Response(
            200,
            content=body.encode(),
            headers={"content-type": media_type},
            request=httpx.Request("POST", "https://vllm.test/v1/audio/transcriptions"),
        )

    def test_text_document_is_not_a_json_envelope(self):
        """``response_format=text`` returns the raw transcript as text/plain."""
        from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse

        result = HostedVLLMAudioTranscriptionConfig().transform_audio_transcription_response(
            self._document_response("hello world", "text/plain")
        )
        assert isinstance(result, NativeAudioDocumentResponse)
        assert result.body == "hello world"
        assert result.media_type == "text/plain"
        assert not result.body.lstrip().startswith("{"), "text response must not be a JSON wrapper"

    def test_srt_document_keeps_native_media_type(self):
        from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse

        srt = "1\n00:00:00,000 --> 00:00:01,000\nhello world\n"
        result = HostedVLLMAudioTranscriptionConfig().transform_audio_transcription_response(
            self._document_response(srt, "application/x-subrip")
        )
        assert isinstance(result, NativeAudioDocumentResponse)
        assert result.body == srt
        assert result.media_type == "application/x-subrip"

    def test_vtt_document_keeps_native_media_type(self):
        from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse

        vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nhello world\n"
        result = HostedVLLMAudioTranscriptionConfig().transform_audio_transcription_response(
            self._document_response(vtt, "text/vtt")
        )
        assert isinstance(result, NativeAudioDocumentResponse)
        assert result.body == vtt
        assert result.media_type == "text/vtt"