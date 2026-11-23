"""NVIDIA NIM native audio transcription config.

Serves the OpenAI-compatible ``/v1/audio/transcriptions`` on the NIM
transcribe service (e.g. nemotron-transcribe). The wire shape is composed
from the same shared native multipart helper as hosted-vLLM, but this class
is a sibling, not a subclass: NIM is different serving software with its own
auth, api base, error class, and a narrower parameter surface. Activation is
gated by the controller's compound NIM identity check (server name + tag +
mounted paths), never by a checkpoint name.
"""

from collections.abc import Mapping
from typing import Final, Optional

import httpx

from litellm.llms.base_llm.audio_transcription.native_multipart import (
    OpenAICompatibleNativeAudioMultipartTransformation,
)
from litellm.llms.base_llm.audio_transcription.transformation import (
    AudioTranscriptionRequestData,
    BaseAudioTranscriptionConfig,
)
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.audio_transcription import NativeTranscriptEvent
from litellm.types.llms.openai import (
    AllMessageValues,
    OpenAIAudioTranscriptionOptionalParams,
)
from litellm.types.utils import FileTypes, TranscriptionResponse


class NvidiaNimAudioTranscriptionError(BaseLLMException):
    def __init__(
        self,
        status_code: int,
        message: str,
        headers: dict | httpx.Headers | None = None,
    ):
        super().__init__(status_code=status_code, message=message, headers=headers)


class NvidiaNimAudioTranscriptionConfig(BaseAudioTranscriptionConfig):
    _native_multipart: Final = OpenAICompatibleNativeAudioMultipartTransformation()

    @property
    def uses_native_multipart_transport(self) -> bool:
        return True

    @property
    def supports_multipart_streaming(self) -> bool:
        """The NIM transcribe service answers ``stream=true`` with an SSE body
        of typed ``transcript.text.delta`` frames and one terminal
        ``transcript.text.done`` frame, verified live against the serving
        pod."""
        return True

    def transform_streaming_event(
        self,
        model: str,
        parsed_json: Mapping[str, object],
    ) -> Optional[NativeTranscriptEvent]:
        from litellm.llms.nvidia_nim.audio_transcription.streaming import (
            transform_streaming_audio_response,
        )

        return transform_streaming_audio_response(model, parsed_json)

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
            return f"{base}/audio/transcriptions"
        return f"{base}/v1/audio/transcriptions"

    def get_supported_openai_params(self, model: str) -> list[OpenAIAudioTranscriptionOptionalParams]:
        return [
            "language",
            "prompt",
            "response_format",
            "temperature",
            "timestamp_granularities",
        ]

    def map_openai_params(
        self,
        non_default_params: dict,
        optional_params: dict,
        model: str,
        drop_params: bool,
    ) -> dict:
        supported_params: Final = self.get_supported_openai_params(model)
        for k, v in non_default_params.items():
            if k in supported_params:
                optional_params[k] = v
        return optional_params

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
        api_key = api_key or get_secret_str("NVIDIA_NIM_API_KEY")
        if api_key:
            headers.update({"Authorization": f"Bearer {api_key}"})
        return headers

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
    ) -> TranscriptionResponse:
        return self._native_multipart.parse_native_audio_response(raw_response)

    def get_error_class(self, error_message: str, status_code: int, headers: dict | httpx.Headers) -> BaseLLMException:
        return NvidiaNimAudioTranscriptionError(
            status_code=status_code,
            message=error_message,
            headers=headers,
        )
