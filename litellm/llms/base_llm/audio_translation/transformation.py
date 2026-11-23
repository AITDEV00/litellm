"""Base contract for native audio translation provider configs.

Translation is its own first-class operation (the plan's D7): a distinct call
type, URL path (``/audio/translations``), response hooks, cost lookup, and
NIM's ``target_language`` parameter. It never rides the transcription SDK
with a swapped suffix. The wire shape (multipart file + form fields) is shared
with transcription through the same native multipart helper, but the config
classes are siblings, not a hierarchy: a provider declares both capabilities
separately and they can diverge (the live NIM supports transcription
streaming but rejects translation streaming with a 400, and that difference
must stay truthful).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Final

import httpx

from litellm.llms.base_llm.chat.transformation import BaseConfig
from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse
from litellm.types.llms.openai import AllMessageValues
from litellm.types.utils import FileTypes, ModelResponse, TranscriptionResponse


@dataclass
class AudioTranslationRequestData:
    """Structured data for audio translation requests, mirroring the
    transcription request data contract: form fields for multipart (scalar
    value = one part, list value = repeated parts), the upload split into
    ``files``."""

    data: dict | bytes
    files: dict | None = None
    content_type: str | None = None


class BaseAudioTranslationConfig(BaseConfig, ABC):
    """Native audio translation against an OpenAI-compatible ``/audio/translations``.

    Providers whose serving stack exposes the standard OpenAI translation
    surface (hosted-vLLM style servers, the NVIDIA NIM transcribe service)
    implement this. Plain OpenAI keeps its official SDK transport and does
    not need this class.
    """

    @abstractmethod
    def get_supported_openai_params(self, model: str) -> list[str]:
        """Standard OpenAI translation parameters this server accepts."""

    @property
    def uses_native_multipart_transport(self) -> bool:
        """Opt-in for translation configs whose multipart body is built from
        the typed native form-field contract instead of the OpenAI SDK request
        shape. Provider identity chooses this, never the model name."""
        return False

    def map_openai_params(
        self,
        non_default_params: dict,
        optional_params: dict,
        model: str,
        drop_params: bool,
    ) -> dict:
        """Map the standard OpenAI translation params into the request.

        Default implementation keeps supported params as-is, mirroring the
        transcription configs.
        """
        supported_params: Final = self.get_supported_openai_params(model)
        for k, v in non_default_params.items():
            if k in supported_params:
                optional_params[k] = v
        return optional_params

    def get_complete_url(
        self,
        api_base: str | None,
        api_key: str | None,
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: bool | None = None,
    ) -> str:
        """The full translation URL. Providers override for their own path."""
        return api_base or ""

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
        return headers

    @abstractmethod
    def transform_audio_translation_request(
        self,
        model: str,
        audio_file: FileTypes,
        optional_params: dict,
        litellm_params: dict,
    ) -> AudioTranslationRequestData:
        raise NotImplementedError("AudioTranslationConfig needs a request transformation for audio translation models")

    @abstractmethod
    def transform_audio_translation_response(
        self,
        raw_response: httpx.Response,
    ) -> TranscriptionResponse | NativeAudioDocumentResponse:
        """Parse the provider response.

        Returns a ``TranscriptionResponse`` for JSON bodies (translation
        responses are the same public shape) or a
        ``NativeAudioDocumentResponse`` for text/SRT/VTT bodies.
        """
        raise NotImplementedError("AudioTranslationConfig needs a response transformation for audio translation models")

    def get_error_class(self, error_message: str, status_code: int, headers: dict | httpx.Headers) -> Any:
        """The provider's exception class for upstream errors."""
        raise NotImplementedError("AudioTranslationConfig needs an error class for audio translation models")

    def transform_request(
        self,
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> dict:
        raise NotImplementedError("AudioTranslationConfig does not need a chat request transformation")

    def transform_response(
        self,
        model: str,
        raw_response: httpx.Response,
        model_response: ModelResponse,
        logging_obj: Any,
        request_data: dict,
        messages: list[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        encoding: Any | None,
        api_key: str | None = None,
        json_mode: bool | None = None,
    ) -> ModelResponse:
        raise NotImplementedError("AudioTranslationConfig does not need a chat response transformation")
