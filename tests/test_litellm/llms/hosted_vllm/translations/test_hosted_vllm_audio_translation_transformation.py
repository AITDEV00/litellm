"""Behavior tests for HostedVLLMAudioTranslationConfig request/response transformation.

Pins the hosted-vLLM translation wire contract: the OpenAI translation
parameter surface (``prompt``, ``response_format``, ``temperature``), native
extension fields preserved top-level, the URL resolving to
``/v1/audio/translations``, and the shared content-aware response parsing
(JSON hydrates, SRT stays a document). Sibling of the transcription suite;
the two must not drift apart.
"""

import io
import json

import httpx

from litellm.llms.base_llm.audio_translation.transformation import AudioTranslationRequestData
from litellm.llms.hosted_vllm.audio_translation.transformation import (
    HostedVLLMAudioTranslationConfig,
)


def _field_dict(result) -> dict[str, object]:
    return dict(result.data)


def _audio() -> io.BytesIO:
    audio = io.BytesIO(b"fake audio bytes")
    audio.name = "meeting.wav"
    return audio


def _response(status_code: int, headers: dict, content: bytes) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers=headers,
        content=content,
        request=httpx.Request("POST", "http://vllm/v1/audio/translations"),
    )


class TestHostedVLLMAudioTranslationRequest:
    def _transform(self, optional_params: dict):
        return HostedVLLMAudioTranslationConfig().transform_audio_translation_request(
            model="Qwen/Qwen3-ASR-1.7B",
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

    def test_standard_params_and_model_reach_the_wire(self):
        result = self._transform({"prompt": "verbatim", "temperature": 0, "response_format": "text"})
        fields = _field_dict(result)
        assert fields["model"] == "Qwen/Qwen3-ASR-1.7B"
        assert fields["prompt"] == "verbatim"
        assert fields["response_format"] == "text"
        assert result.files is not None and "file" in result.files

    def test_native_extension_fields_are_preserved_top_level(self):
        result = self._transform({"target_language": "fr", "chunk_offset": 5})
        fields = _field_dict(result)
        assert fields["target_language"] == "fr"
        assert fields["chunk_offset"] == 5

    def test_language_is_not_injected_for_translation(self):
        """hosted-vLLM translation has no source-language param; nothing may alias one in."""
        result = self._transform({})
        assert "language" not in _field_dict(result)


class TestHostedVLLMAudioTranslationURL:
    def test_url_appends_v1_when_missing(self):
        assert (
            HostedVLLMAudioTranslationConfig().get_complete_url(
                api_base="http://vllm:8000",
                api_key=None,
                model="m",
                optional_params={},
                litellm_params={},
            )
            == "http://vllm:8000/v1/audio/translations"
        )

    def test_url_does_not_double_v1(self):
        assert (
            HostedVLLMAudioTranslationConfig().get_complete_url(
                api_base="http://vllm:8000/v1/",
                api_key=None,
                model="m",
                optional_params={},
                litellm_params={},
            )
            == "http://vllm:8000/v1/audio/translations"
        )


class TestHostedVLLMAudioTranslationResponse:
    def test_json_body_hydrates_transcription_response(self):
        payload = {"text": "bonjour", "duration": 1.25}
        result = HostedVLLMAudioTranslationConfig().transform_audio_translation_response(
            _response(200, {"content-type": "application/json"}, json.dumps(payload).encode())
        )
        assert result.text == "bonjour"
        assert getattr(result, "duration") == 1.25

    def test_srt_body_stays_a_true_document(self):
        srt = "1\n00:00:00,000 --> 00:00:01,000\nbonjour\n"
        result = HostedVLLMAudioTranslationConfig().transform_audio_translation_response(
            _response(200, {"content-type": "application/x-subrip"}, srt.encode())
        )
        from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse

        assert isinstance(result, NativeAudioDocumentResponse)
        assert result.body == srt
        assert result.media_type == "application/x-subrip"

    def test_vtt_body_with_charset_decodes_correctly(self):
        vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nbonjour\n"
        result = HostedVLLMAudioTranslationConfig().transform_audio_translation_response(
            _response(200, {"content-type": "text/vtt; charset=utf-8"}, vtt.encode())
        )
        from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse

        assert isinstance(result, NativeAudioDocumentResponse)
        assert result.body == vtt
        assert result.charset == "utf-8"
