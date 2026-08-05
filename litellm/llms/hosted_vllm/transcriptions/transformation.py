"""
Transformation logic for Hosted VLLM audio transcription.
"""

from typing import Optional, Union

import httpx

from litellm.litellm_core_utils.audio_utils.utils import process_audio_file
from litellm.llms.base_llm.audio_transcription.transformation import (
    AudioTranscriptionRequestData,
)
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.openai.transcriptions.whisper_transformation import (
    OpenAIWhisperAudioTranscriptionConfig,
)
from litellm.types.utils import FileTypes


class HostedVLLMAudioTranscriptionError(BaseLLMException):
    def __init__(
        self,
        status_code: int,
        message: str,
        headers: Optional[Union[dict, httpx.Headers]] = None,
    ):
        super().__init__(status_code=status_code, message=message, headers=headers)


class HostedVLLMAudioTranscriptionConfig(OpenAIWhisperAudioTranscriptionConfig):
    def get_complete_url(
        self,
        api_base: Optional[str],
        api_key: Optional[str],
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: Optional[bool] = None,
    ) -> str:
        url = super().get_complete_url(
            api_base=api_base,
            api_key=api_key,
            model=model,
            optional_params=optional_params,
            litellm_params=litellm_params,
            stream=stream,
        )
        if not url:
            raise ValueError("api_base must be provided for Hosted VLLM audio transcription")
        return url

    def transform_audio_transcription_request(
        self,
        model: str,
        audio_file: FileTypes,
        optional_params: dict,
        litellm_params: dict,
    ) -> AudioTranscriptionRequestData:
        """
        Transform the audio transcription request.

        Hosted VLLM is an OpenAI-compatible endpoint reached through
        ``base_llm_http_handler``, which sends multipart form data via httpx
        (``data=`` for the form fields plus ``files=`` for the upload). The
        inherited OpenAI whisper transform instead puts the raw file object
        inside ``data["file"]`` with ``files=None``; that works only for the
        OpenAI SDK path. Here we must split the file out into the ``files``
        dict (a ``(filename, content, content_type)`` tuple) exactly like the
        other httpx-based providers, otherwise httpx JSON-serializes ``data``
        and fails with "Object of type BytesIO is not JSON serializable".
        """
        processed_audio = process_audio_file(audio_file)

        form_fields: dict = {
            "model": model,
        }

        for key in self.get_supported_openai_params(model):
            value = optional_params.get(key)
            if value is not None:
                form_fields[key] = value

        files = {
            "file": (
                processed_audio.filename,
                processed_audio.file_content,
                processed_audio.content_type,
            )
        }

        return AudioTranscriptionRequestData(
            data=form_fields,
            files=files,
        )
