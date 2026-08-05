"""
Tests for HostedVLLMAudioTranscriptionConfig.transform_audio_transcription_request.

Hosted VLLM is reached through ``base_llm_http_handler``, which POSTs multipart
form data via httpx using ``data=`` (form fields) plus ``files=`` (the upload).
The inherited OpenAI whisper transform instead nests the raw file object inside
``data["file"]`` with ``files=None``, which makes httpx JSON-serialize the body
and fail with "Object of type BytesIO is not JSON serializable". These tests pin
the fix: the audio file must be split out into the ``files`` dict as a
``(filename, content, content_type)`` tuple, leaving ``data`` JSON-safe.
"""

import io
import json

from litellm.llms.base_llm.audio_transcription.transformation import (
    AudioTranscriptionRequestData,
)
from litellm.llms.hosted_vllm.transcriptions.transformation import (
    HostedVLLMAudioTranscriptionConfig,
)


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

        # data must contain no file object and be JSON-safe form fields only
        assert "file" not in result.data
        assert set(result.data) == {"model", "temperature"}
        assert result.data["model"] == "Qwen/Qwen3-ASR-1.7B"
        assert result.data["temperature"] == 0.0

        # the file must be present as a multipart (filename, content, content_type) tuple
        assert result.files is not None
        assert "file" in result.files
        filename, content, content_type = result.files["file"]
        assert filename
        assert content == b"fake audio bytes"
        assert content_type

        # the whole payload must remain JSON-serializable after dropping files
        json.dumps(result.data)

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
        assert result.data["language"] == "en"
        assert result.data["temperature"] == 0.0
        assert result.data["response_format"] == "json"
        assert result.data["prompt"] == "transcribe carefully"

    def test_ignores_unsupported_optional_params(self):
        """Params outside the OpenAI whisper allow-list are dropped from the form."""
        result = self._transform(io.BytesIO(b"audio"), {"diarize": True, "nonsense": 42})
        assert "diarize" not in result.data
        assert "nonsense" not in result.data
        assert set(result.data) == {"model"}

    def test_handles_filename_carrying_bytesio(self):
        """A BytesIO with a .name attribute uses that filename and a sensible content type."""
        buf = io.BytesIO(b"abc")
        buf.name = "clip.mp3"
        result = self._transform(buf, {})
        filename, content, content_type = result.files["file"]
        assert filename == "clip.mp3"
        assert content == b"abc"
        assert content_type