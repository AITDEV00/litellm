from typing import Any, Dict, Optional, Tuple, Union

import httpx

from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.base_llm.text_to_speech.transformation import (
    BaseTextToSpeechConfig,
    TextToSpeechRequestData,
)
from litellm.llms.hamsa.common_utils import HAMSA_INTERNAL_PARAMS, HamsaModelInfo


class HamsaTextToSpeechConfig(HamsaModelInfo, BaseTextToSpeechConfig):
    def get_supported_openai_params(self, model: str) -> list:
        return ["voice", "response_format", "speed"]

    def map_openai_params(
        self,
        model: str,
        optional_params: Dict,
        voice: Optional[Union[str, Dict]] = None,
        drop_params: bool = False,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[str], Dict]:
        mapped_params: Dict[str, Any] = {}

        speaker: Optional[str] = None
        if isinstance(voice, str):
            speaker = voice
        elif isinstance(voice, dict):
            speaker = voice.get("voice_id") or voice.get("id") or voice.get("name")
        elif voice is not None:
            speaker = str(voice)

        if speaker is None:
            raise BaseLLMException(
                status_code=400,
                message="'voice' (speaker) is required for Hamsa TTS. Pass a speaker name like 'jasem'.",
                headers={},
            )

        response_format = optional_params.pop("response_format", None)
        if isinstance(response_format, str):
            mapped_params["mulaw"] = response_format.lower() == "mulaw"

        speed = optional_params.pop("speed", None)
        if speed is not None:
            mapped_params["speed"] = speed

        optional_params.pop("instructions", None)

        extra_body = optional_params.pop("extra_body", None)
        if isinstance(extra_body, dict):
            for key, value in extra_body.items():
                if value is None:
                    continue
                mapped_params[key] = value

        if kwargs is not None:
            for key, value in kwargs.items():
                if value is None or key in HAMSA_INTERNAL_PARAMS:
                    continue
                if key in ("extra_body", "extra_headers"):
                    continue
                mapped_params[key] = value

        for key, value in optional_params.items():
            if value is None or key in HAMSA_INTERNAL_PARAMS:
                continue
            mapped_params[key] = value

        return speaker, mapped_params

    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> dict:
        return self._inject_auth_headers(headers, api_key)

    def get_complete_url(
        self,
        model: str,
        api_base: Optional[str],
        litellm_params: dict,
    ) -> str:
        base = self.get_api_base(api_base)
        if base is None:
            raise BaseLLMException(
                status_code=400,
                message="Missing Hamsa API base. Set HAMSA_API_BASE or pass api_base in model config.",
                headers={},
            )
        return base.rstrip("/") + "/tts/stream"

    def transform_text_to_speech_request(
        self,
        model: str,
        input: str,
        voice: Optional[str],
        optional_params: Dict,
        litellm_params: Dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        request_body: Dict[str, Any] = {
            "text": input,
            "speaker": voice,
            "language_id": optional_params.pop("language_id", "ar"),
            "stream": False,
        }

        if "mulaw" in optional_params:
            request_body["mulaw"] = optional_params.pop("mulaw")
        if "dialect" in optional_params:
            request_body["dialect"] = optional_params.pop("dialect")
        if "expressiveness" in optional_params:
            request_body["expressiveness"] = optional_params.pop("expressiveness")
        if "speed" in optional_params:
            request_body["speed"] = optional_params.pop("speed")
        if "lang" in optional_params:
            request_body["lang"] = optional_params.pop("lang")

        for key, value in optional_params.items():
            if value is None or key in HAMSA_INTERNAL_PARAMS:
                continue
            request_body[key] = value

        return TextToSpeechRequestData(
            dict_body=request_body,
            headers={"Content-Type": "application/json"},
        )

    def transform_text_to_speech_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: Any,
    ) -> "HttpxBinaryResponseContent":
        from litellm.types.llms.openai import HttpxBinaryResponseContent

        return HttpxBinaryResponseContent(raw_response)
