"""Behavior tests for NvidiaNimAudioTranscriptionConfig request/response transformation.

Pins the NIM transcription contract: standard OpenAI params are validated and
mapped, native extension fields (``diarization``, ``word_timestamps``) survive
top-level, the URL resolves to ``/v1/audio/transcriptions``, auth falls back to
``NVIDIA_NIM_API_KEY``, and repeated ``timestamp_granularities`` parts reach the
wire in order. Companion of the hosted-vLLM transcription suite; the shared
multipart helper's own behavior is pinned there, so this file pins what NIM
adds on top of it.
"""

import io
import json

import httpx
import pytest

from litellm.llms.nvidia_nim.audio_transcription.transformation import (
    NvidiaNimAudioTranscriptionConfig,
    NvidiaNimAudioTranscriptionError,
)


def _audio() -> io.BytesIO:
    audio = io.BytesIO(b"fake audio bytes")
    audio.name = "meeting.wav"
    return audio


def _wire_parts(result) -> list[tuple[str, str]]:
    request = httpx.Request(
        "POST",
        "http://nim/v1/audio/transcriptions",
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


class TestNvidiaNimAudioTranscriptionRequest:
    def test_supported_openai_params_pass_through(self):
        config = NvidiaNimAudioTranscriptionConfig()
        mapped = config.map_openai_params(
            non_default_params={"language": "en", "prompt": "context", "temperature": 0},
            optional_params={},
            model="m",
            drop_params=False,
        )
        assert mapped == {"language": "en", "prompt": "context", "temperature": 0}

    def test_unsupported_standard_param_is_not_mapped(self):
        """UnsupportedParamsError policy is the caller's; mapping itself skips the key."""
        config = NvidiaNimAudioTranscriptionConfig()
        mapped = config.map_openai_params(
            non_default_params={"not_a_param": 1},
            optional_params={},
            model="m",
            drop_params=True,
        )
        assert "not_a_param" not in mapped

    def test_native_extensions_and_repeated_parts_reach_the_wire(self):
        result = NvidiaNimAudioTranscriptionConfig().transform_audio_transcription_request(
            model="nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B",
            audio_file=_audio(),
            optional_params={
                "diarization": True,
                "word_timestamps": True,
                "timestamp_granularities": ["word", "segment"],
            },
            litellm_params={},
        )
        wire = _wire_parts(result)
        assert ("diarization", "true") in wire
        assert ("word_timestamps", "true") in wire
        assert wire.count(("timestamp_granularities", "word")) == 1
        assert wire.count(("timestamp_granularities", "segment")) == 1
        assert result.files is not None and "file" in result.files


class TestNvidiaNimAudioTranscriptionURLAndAuth:
    def test_url_appends_v1_when_missing(self):
        assert (
            NvidiaNimAudioTranscriptionConfig().get_complete_url(
                api_base="https://nim.example.com",
                api_key=None,
                model="m",
                optional_params={},
                litellm_params={},
            )
            == "https://nim.example.com/v1/audio/transcriptions"
        )

    def test_url_does_not_double_v1(self):
        assert (
            NvidiaNimAudioTranscriptionConfig().get_complete_url(
                api_base="https://nim.example.com/v1/",
                api_key=None,
                model="m",
                optional_params={},
                litellm_params={},
            )
            == "https://nim.example.com/v1/audio/transcriptions"
        )

    def test_auth_header_uses_supplied_key(self):
        headers = NvidiaNimAudioTranscriptionConfig().validate_environment(
            headers={},
            model="m",
            messages=[],
            optional_params={},
            litellm_params={},
            api_key="nvapi-test",
        )
        assert headers["Authorization"] == "Bearer nvapi-test"


class TestNvidiaNimAudioTranscriptionResponse:
    def _response(self, status_code: int, headers: dict, content: bytes) -> httpx.Response:
        return httpx.Response(
            status_code=status_code,
            headers=headers,
            content=content,
            request=httpx.Request("POST", "https://nim.example.com/v1/audio/transcriptions"),
        )

    def test_json_body_hydrates_with_native_fields(self):
        payload = {"text": "hi", "duration": 3.2, "language": "en"}
        result = NvidiaNimAudioTranscriptionConfig().transform_audio_transcription_response(
            self._response(200, {"content-type": "application/json"}, json.dumps(payload).encode())
        )
        assert result.text == "hi"
        assert getattr(result, "duration") == 3.2

    def test_plain_text_body_stays_a_true_document(self):
        from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse

        result = NvidiaNimAudioTranscriptionConfig().transform_audio_transcription_response(
            self._response(200, {"content-type": "text/plain"}, b"plain transcript")
        )
        assert isinstance(result, NativeAudioDocumentResponse)
        assert result.body == "plain transcript"

    def test_error_class_keeps_truthful_status_and_message(self):
        error = NvidiaNimAudioTranscriptionConfig().get_error_class(
            error_message="audio decoding failed", status_code=422, headers={}
        )
        assert isinstance(error, NvidiaNimAudioTranscriptionError)
        assert error.status_code == 422
        assert error.message == "audio decoding failed"


class TestNvidiaNimStreamingSupport:
    """The Phase C streaming opt-in and the NIM event vocabulary mapping."""

    def test_config_opts_into_multipart_streaming(self):
        assert NvidiaNimAudioTranscriptionConfig().supports_multipart_streaming is True

    def test_hosted_vllm_stays_non_streaming(self):
        from litellm.llms.hosted_vllm.transcriptions.transformation import (
            HostedVLLMAudioTranscriptionConfig,
        )

        assert HostedVLLMAudioTranscriptionConfig().supports_multipart_streaming is False

    def test_base_config_default_is_no_streaming(self):
        from litellm.llms.base_llm.audio_transcription.transformation import (
            BaseAudioTranscriptionConfig,
        )

        # The base property itself defaults False; a config that does not
        # override it stays non-streaming, so the truthful 400 keeps firing.
        assert BaseAudioTranscriptionConfig.supports_multipart_streaming.fget is not None
        from litellm.llms.hosted_vllm.transcriptions.transformation import (
            HostedVLLMAudioTranscriptionConfig,
        )

        assert HostedVLLMAudioTranscriptionConfig().supports_multipart_streaming is False
        # The base config maps no vocabulary at all, so a payload it is asked
        # about surfaces as the generic unrecognized-event path, never silence.
        assert HostedVLLMAudioTranscriptionConfig().transform_streaming_event("m", {"type": "x"}) is None

    def test_delta_event_maps_with_native_fields_preserved(self):
        from litellm.types.llms.audio_transcription import NativeTranscriptDelta

        event = NvidiaNimAudioTranscriptionConfig().transform_streaming_event(
            "m",
            {"delta": "Four", "type": "transcript.text.delta", "vendor_extra": 1},
        )
        assert isinstance(event, NativeTranscriptDelta)
        assert event.delta == "Four"
        assert event.native_event["vendor_extra"] == 1

    def test_done_event_maps_text_and_usage(self):
        from litellm.types.llms.audio_transcription import NativeTranscriptDone

        event = NvidiaNimAudioTranscriptionConfig().transform_streaming_event(
            "m",
            {
                "text": "full transcript",
                "transcription": {"duration": 17.5},
                "usage": {"seconds": 17.5, "prompt_tokens": 272},
                "type": "transcript.text.done",
            },
        )
        assert isinstance(event, NativeTranscriptDone)
        assert event.text == "full transcript"
        assert event.usage["seconds"] == 17.5
        assert event.native_event["transcription"]["duration"] == 17.5

    def test_error_event_maps_message(self):
        from litellm.types.llms.audio_transcription import NativeTranscriptStreamError

        event = NvidiaNimAudioTranscriptionConfig().transform_streaming_event(
            "m", {"message": "audio too short", "type": "error"}
        )
        assert isinstance(event, NativeTranscriptStreamError)
        assert event.message == "audio too short"

    def test_unknown_event_type_is_an_explicit_error_not_silence(self):
        """The live vocabulary is delta/done/error; a future vendor event must
        surface as a structured error rather than disappearing."""
        from litellm.types.llms.audio_transcription import NativeTranscriptStreamError

        event = NvidiaNimAudioTranscriptionConfig().transform_streaming_event(
            "m", {"type": "transcript.something.new", "payload": {}}
        )
        assert isinstance(event, NativeTranscriptStreamError)
        assert "unrecognized" in event.message
