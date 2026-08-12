"""
Transformation logic for Hosted VLLM audio transcription.
"""

import httpx

from litellm.litellm_core_utils.audio_utils.utils import process_audio_file
from litellm.llms.base_llm.audio_transcription.transformation import (
    AudioTranscriptionRequestData,
)
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.openai.transcriptions.whisper_transformation import (
    OpenAIWhisperAudioTranscriptionConfig,
)
from litellm.types.utils import FileTypes, TranscriptionResponse


class HostedVLLMAudioTranscriptionError(BaseLLMException):
    def __init__(
        self,
        status_code: int,
        message: str,
        headers: dict | httpx.Headers | None = None,
    ):
        super().__init__(status_code=status_code, message=message, headers=headers)


class HostedVLLMAudioTranscriptionConfig(OpenAIWhisperAudioTranscriptionConfig):
    def get_complete_url(
        self,
        api_base: str | None,
        api_key: str | None,
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: bool | None = None,
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

    def transform_audio_transcription_response(
        self,
        raw_response: httpx.Response,
    ) -> TranscriptionResponse:
        """
        Transform the audio transcription response.

        The inherited whisper transform calls ``TranscriptionResponse(**json)``,
        which breaks whenever the endpoint returns extra keys (e.g. Qwen ASR
        returns a ``usage`` field) because ``TranscriptionResponse.__init__``
        only accepts ``text``. Here we build the response from ``text`` and copy
        any remaining provider fields onto the object, matching the pattern used
        by the other httpx-based providers (e.g. inception).
        """
        payload = raw_response.json()
        text = payload.get("text", "")
        response = TranscriptionResponse(text=text)

        for key, value in payload.items():
            if key == "text":
                continue
            response[key] = value

        response._hidden_params = payload
        return response
