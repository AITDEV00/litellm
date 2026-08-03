from typing import Optional

from httpx import Headers, Response

from litellm.litellm_core_utils.audio_utils.utils import process_audio_file
from litellm.llms.base_llm.audio_transcription.transformation import (
    AudioTranscriptionRequestData,
    BaseAudioTranscriptionConfig,
)
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.inception.common_utils import (
    INCEPTION_INTERNAL_PARAMS,
    InceptionAudioModelInfo,
)
from litellm.types.llms.openai import (
    AllMessageValues,
    OpenAIAudioTranscriptionOptionalParams,
)
from litellm.types.utils import FileTypes, TranscriptionResponse


class InceptionAudioTranscriptionConfig(InceptionAudioModelInfo, BaseAudioTranscriptionConfig):
    def get_supported_openai_params(self, model: str) -> list[OpenAIAudioTranscriptionOptionalParams]:
        return ["language", "prompt", "response_format", "temperature"]

    def map_openai_params(
        self,
        non_default_params: dict,
        optional_params: dict,
        model: str,
        drop_params: bool,
    ) -> dict:
        for key in ("language", "prompt", "response_format", "temperature"):
            if key in non_default_params and non_default_params[key] is not None:
                optional_params[key] = non_default_params[key]

        if "timestamp_granularities" in non_default_params and non_default_params["timestamp_granularities"]:
            optional_params["timestamp_granularities"] = non_default_params["timestamp_granularities"]

        for key, value in non_default_params.items():
            if value is None or key in INCEPTION_INTERNAL_PARAMS:
                continue
            if key in ("language", "prompt", "response_format", "temperature", "timestamp_granularities"):
                continue
            optional_params[key] = value
        return optional_params

    def get_error_class(self, error_message: str, status_code: int, headers: dict | Headers) -> BaseLLMException:
        raise BaseLLMException(status_code=status_code, message=error_message, headers=headers)

    def validate_environment(
        self,
        headers: dict,
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict,
        litellm_params: dict,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> dict:
        return self._inject_auth_headers(headers, api_key)

    def get_complete_url(
        self,
        api_base: Optional[str],
        api_key: Optional[str],
        model: str,
        optional_params: dict,
        litellm_params: dict,
        stream: Optional[bool] = None,
    ) -> str:
        return self._resolve_base(api_base) + "/v1/audio/transcriptions"

    def transform_audio_transcription_request(
        self,
        model: str,
        audio_file: FileTypes,
        optional_params: dict,
        litellm_params: dict,
    ) -> AudioTranscriptionRequestData:
        processed = process_audio_file(audio_file)

        form_fields: dict = {"model": model}
        for key, value in optional_params.items():
            if value is None or key in INCEPTION_INTERNAL_PARAMS:
                continue
            form_fields[key] = value

        files = {
            "file": (processed.filename, processed.file_content, processed.content_type),
        }

        return AudioTranscriptionRequestData(data=form_fields, files=files)

    def transform_audio_transcription_response(
        self,
        raw_response: Response,
    ) -> TranscriptionResponse:
        payload = raw_response.json()
        text = payload.get("text", "")
        response = TranscriptionResponse(text=text)
        response["task"] = "transcribe"

        for key, value in payload.items():
            if key == "text":
                continue
            response[key] = value

        response._hidden_params = payload
        return response
