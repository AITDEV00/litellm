"""
Transformation logic for Hosted VLLM audio transcription.
"""

from typing import Final

import httpx

from litellm.llms.base_llm.audio_transcription.native_multipart import (
    OpenAICompatibleNativeAudioMultipartTransformation,
)
from litellm.llms.base_llm.audio_transcription.transformation import (
    AudioTranscriptionRequestData,
)
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.openai.transcriptions.whisper_transformation import (
    OpenAIWhisperAudioTranscriptionConfig,
)
from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse
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
    """Transcription against an OpenAI-compatible vLLM/sglang server.

    The wire shape is delegated to the shared native multipart helper (ordered
    scalar form fields, repeated parts, unpacked ``extra_body``) so
    vendor/extension params like ``diarization`` reach the server instead of
    being discarded by the standard-parameter allowlist. This class keeps the
    hosted-vLLM identity: URL construction, auth, error class, and the
    standard OpenAI parameters it supports.
    """

    _native_multipart: Final = OpenAICompatibleNativeAudioMultipartTransformation()

    @property
    def uses_native_multipart_transport(self) -> bool:
        return True

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
        return self._native_multipart.build_native_multipart_request(
            model=model,
            audio_file=audio_file,
            optional_params=optional_params,
            supported_openai_params=self.get_supported_openai_params(model),
        )

    def transform_audio_transcription_response(
        self,
        raw_response: httpx.Response,
    ) -> TranscriptionResponse | NativeAudioDocumentResponse:
        return self._native_multipart.parse_native_audio_response(raw_response)

    def get_error_class(self, error_message: str, status_code: int, headers: dict | httpx.Headers) -> BaseLLMException:
        return HostedVLLMAudioTranscriptionError(
            status_code=status_code,
            message=error_message,
            headers=headers,
        )
