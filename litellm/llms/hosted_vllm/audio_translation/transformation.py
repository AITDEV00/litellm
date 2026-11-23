"""Hosted VLLM native audio translation config.

The served surface is the standard OpenAI-compatible ``/audio/translations``:
multipart ``file`` + ``model`` + standard translation params (``prompt``,
``response_format``, ``temperature``) plus whatever native extension params
the server accepts. The wire shape is delegated to the shared native
multipart helper so behavior matches transcription exactly; this class owns
only the URL path and its identity.
"""

from typing import Final

import httpx

from litellm.llms.base_llm.audio_transcription.native_multipart import (
    OpenAICompatibleNativeAudioMultipartTransformation,
)
from litellm.llms.base_llm.audio_translation.transformation import (
    AudioTranslationRequestData,
    BaseAudioTranslationConfig,
)
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse
from litellm.types.llms.openai import AllMessageValues
from litellm.types.utils import FileTypes, TranscriptionResponse


class HostedVLLMAudioTranslationError(BaseLLMException):
    def __init__(
        self,
        status_code: int,
        message: str,
        headers: dict | httpx.Headers | None = None,
    ) -> None:
        super().__init__(status_code=status_code, message=message, headers=headers)


class HostedVLLMAudioTranslationConfig(BaseAudioTranslationConfig):
    _native_multipart: Final = OpenAICompatibleNativeAudioMultipartTransformation()

    @property
    def uses_native_multipart_transport(self) -> bool:
        return True

    def get_supported_openai_params(self, model: str) -> list[str]:
        """The OpenAI translation parameter surface."""
        return [
            "prompt",
            "response_format",
            "temperature",
        ]

    def get_complete_url(
        self,
        api_base: str | None,
        api_key: str | None,
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: bool | None = None,
    ) -> str:
        base = api_base.rstrip("/") if api_base else ""
        if base.endswith("/v1"):
            return f"{base}/audio/translations"
        return f"{base}/v1/audio/translations"

    def validate_environment(
        self,
        headers: dict,
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> dict:
        api_key = api_key or get_secret_str("HOSTED_VLLM_API_KEY")
        if api_key:
            headers.update({"Authorization": f"Bearer {api_key}"})
        return headers

    def transform_audio_translation_request(
        self,
        model: str,
        audio_file: FileTypes,
        optional_params: dict,
        litellm_params: dict,
    ) -> AudioTranslationRequestData:
        built: Final = self._native_multipart.build_native_multipart_request(
            model=model,
            audio_file=audio_file,
            optional_params=optional_params,
            supported_openai_params=self.get_supported_openai_params(model),
        )
        return AudioTranslationRequestData(
            data=built.data,
            files=built.files,
            content_type=built.content_type,
        )

    def transform_audio_translation_response(
        self, raw_response: httpx.Response
    ) -> TranscriptionResponse | NativeAudioDocumentResponse:
        return self._native_multipart.parse_native_audio_response(raw_response)

    def get_error_class(self, error_message: str, status_code: int, headers: dict | httpx.Headers) -> BaseLLMException:
        return HostedVLLMAudioTranslationError(
            status_code=status_code,
            message=error_message,
            headers=headers,
        )
