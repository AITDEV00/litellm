import base64
import json
from typing import List, Optional, Union

from httpx import Headers, Response

from litellm.litellm_core_utils.audio_utils.utils import process_audio_file
from litellm.llms.base_llm.audio_transcription.transformation import (
    AudioTranscriptionRequestData,
    BaseAudioTranscriptionConfig,
)
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.hamsa.common_utils import HAMSA_INTERNAL_PARAMS, HamsaModelInfo
from litellm.types.llms.openai import (
    AllMessageValues,
    OpenAIAudioTranscriptionOptionalParams,
)
from litellm.types.utils import FileTypes, TranscriptionResponse


class HamsaAudioTranscriptionConfig(HamsaModelInfo, BaseAudioTranscriptionConfig):
    def get_supported_openai_params(self, model: str) -> List[OpenAIAudioTranscriptionOptionalParams]:
        return ["language", "prompt"]

    def map_openai_params(
        self,
        non_default_params: dict,
        optional_params: dict,
        model: str,
        drop_params: bool,
    ) -> dict:
        if "language" in non_default_params and non_default_params["language"]:
            optional_params["lang"] = non_default_params["language"]
        if "prompt" in non_default_params and non_default_params["prompt"]:
            optional_params["prompt"] = non_default_params["prompt"]
        for key, value in non_default_params.items():
            if value is None or key in HAMSA_INTERNAL_PARAMS or key in ("language", "prompt"):
                continue
            optional_params[key] = value
        return optional_params

    def get_error_class(self, error_message: str, status_code: int, headers: Union[dict, Headers]) -> BaseLLMException:
        raise BaseLLMException(status_code=status_code, message=error_message, headers=headers)

    def validate_environment(
        self,
        headers: dict,
        model: str,
        messages: List[AllMessageValues],
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
        base = self.get_api_base(api_base)
        if base is None:
            raise BaseLLMException(
                status_code=400,
                message="Missing Hamsa API base. Set HAMSA_API_BASE or pass api_base in model config.",
                headers={},
            )
        return base.rstrip("/") + "/transcribe"

    def transform_audio_transcription_request(
        self,
        model: str,
        audio_file: FileTypes,
        optional_params: dict,
        litellm_params: dict,
    ) -> AudioTranscriptionRequestData:
        processed = process_audio_file(audio_file)
        audio_b64 = base64.b64encode(processed.file_content).decode("utf-8")

        body: dict = {"audio": audio_b64}
        for key, value in optional_params.items():
            if value is None or key in HAMSA_INTERNAL_PARAMS:
                continue
            body[key] = value

        json_bytes = json.dumps(body).encode("utf-8")
        return AudioTranscriptionRequestData(data=json_bytes, files=None, content_type="application/json")

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
