"""Behavior tests for NvidiaNimAudioTranslationConfig request/response transformation.

Pins the NIM translation contract: ``target_language`` is preserved as its own
top-level form field (never aliased to the transcription-side ``language``),
native extension fields survive the mapping, the URL resolves to
``/v1/audio/translations`` for both bare and /v1-suffixed api bases, the auth
fallback reads ``NVIDIA_NIM_API_KEY``, and JSON responses hydrate through the
shared hydrator while text/SRT documents stay true documents.
"""

import io
import json

import httpx
import pytest

from litellm.llms.base_llm.audio_translation.transformation import AudioTranslationRequestData
from litellm.llms.nvidia_nim.audio_translation.transformation import (
    NvidiaNimAudioTranslationConfig,
)


def _field_dict(result) -> dict[str, object]:
    return dict(result.data)


def _wire_parts(result) -> list[tuple[str, str]]:
    """Encode exactly as httpx sends (data= + files=) and parse the wire body back."""
    request = httpx.Request(
        "POST",
        "http://nim/v1/audio/translations",
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


def _audio() -> io.BytesIO:
    audio = io.BytesIO(b"fake audio bytes")
    audio.name = "meeting.wav"
    return audio


class TestNvidiaNimAudioTranslationRequest:
    def _transform(self, optional_params: dict):
        return NvidiaNimAudioTranslationConfig().transform_audio_translation_request(
            model="nvidia/parakeet-mlx-v3",
            audio_file=_audio(),
            optional_params=optional_params,
            litellm_params={},
        )

    def test_request_transform_returns_the_translation_envelope(self):
        """The HTTP handler isinstance-gates on AudioTranslationRequestData; the
        shared multipart builder returns the transcription envelope, so this
        config must re-wrap or every gateway translation 500s."""
        result = self._transform({"target_language": "en"})
        assert isinstance(result, AudioTranslationRequestData)

    def test_target_language_is_its_own_field_not_aliased_to_language(self):
        result = self._transform({"target_language": "en"})
        fields = _field_dict(result)
        assert fields["target_language"] == "en"
        assert "language" not in fields, "translation must not alias target_language to the transcription source language"

    def test_standard_translation_params_reach_the_wire(self):
        result = self._transform({"prompt": "keep names", "temperature": 0.1, "response_format": "json"})
        fields = _field_dict(result)
        assert fields["prompt"] == "keep names"
        assert fields["temperature"] == 0.1
        assert fields["response_format"] == "json"

    def test_model_and_file_reach_the_wire(self):
        result = self._transform({})
        wire = _wire_parts(result)
        assert ("model", "nvidia/parakeet-mlx-v3") in wire
        assert result.files is not None and "file" in result.files

    def test_stream_true_is_forwarded_so_the_upstream_400_stays_truthful(self):
        """stream rides through as a native field: the inspected NIM rejects it
        with its own 400, and the plan pins that upstream error as the contract.
        Swallowing or buffering it here would rewrite the provider's truth."""
        result = self._transform({"stream": True})
        assert _field_dict(result)["stream"] is True


class TestNvidiaNimAudioTranslationURLAndAuth:
    def test_url_appends_v1_when_missing(self):
        config = NvidiaNimAudioTranslationConfig()
        assert (
            config.get_complete_url(
                api_base="https://nim.example.com",
                api_key=None,
                model="m",
                optional_params={},
                litellm_params={},
            )
            == "https://nim.example.com/v1/audio/translations"
        )

    def test_url_does_not_double_v1(self):
        config = NvidiaNimAudioTranslationConfig()
        assert (
            config.get_complete_url(
                api_base="https://nim.example.com/v1/",
                api_key=None,
                model="m",
                optional_params={},
                litellm_params={},
            )
            == "https://nim.example.com/v1/audio/translations"
        )

    def test_auth_header_uses_supplied_key(self):
        config = NvidiaNimAudioTranslationConfig()
        headers = config.validate_environment(
            headers={},
            model="m",
            messages=[],
            optional_params={},
            litellm_params={},
            api_key="nvapi-test",
        )
        assert headers["Authorization"] == "Bearer nvapi-test"


class TestNvidiaNimAudioTranslationResponse:
    def _response(self, status_code: int, headers: dict, content: bytes) -> httpx.Response:
        return httpx.Response(
            status_code=status_code,
            headers=headers,
            content=content,
            request=httpx.Request("POST", "https://nim.example.com/v1/audio/translations"),
        )

    def test_json_body_hydrates_transcription_response_with_native_fields(self):
        payload = {"text": "hello", "duration": 2.5, "language": "en"}
        result = NvidiaNimAudioTranslationConfig().transform_audio_translation_response(
            self._response(200, {"content-type": "application/json"}, json.dumps(payload).encode())
        )
        assert result.text == "hello"
        assert getattr(result, "duration") == 2.5

    def test_plain_text_body_stays_a_true_document(self):
        result = NvidiaNimAudioTranslationConfig().transform_audio_translation_response(
            self._response(200, {"content-type": "text/plain"}, b"hello translated")
        )
        from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse

        assert isinstance(result, NativeAudioDocumentResponse)
        assert result.body == "hello translated"
        assert result.media_type == "text/plain"

    def test_error_mapping_produces_own_error_class_with_truthful_status(self):
        """4xx surfaces through _handle_error -> get_error_class, keeping the
        upstream status and message verbatim (no rewritten error envelope)."""
        from litellm.llms.nvidia_nim.audio_translation.transformation import (
            NvidiaNimAudioTranslationError,
        )

        config = NvidiaNimAudioTranslationConfig()
        error = config.get_error_class(
            error_message="Streaming is not supported for audio translations",
            status_code=400,
            headers={},
        )
        assert isinstance(error, NvidiaNimAudioTranslationError)
        assert error.status_code == 400
        assert "Streaming is not supported" in error.message
