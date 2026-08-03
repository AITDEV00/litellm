from typing import Any, Optional

import httpx

from litellm.llms.base_llm.text_to_speech.transformation import (
    BaseTextToSpeechConfig,
    TextToSpeechRequestData,
)
from litellm.llms.omnivoice.common_utils import (
    OmniVoiceModelInfo,
    _collect_passthrough,
)

_TTS_FORM_KEYS: tuple[str, ...] = ("response_format", "speed", "language", "stream")


def _resolve_voice(voice: Optional[str | dict]) -> Optional[str]:
    if isinstance(voice, str):
        return voice
    if isinstance(voice, dict):
        return voice.get("voice_id") or voice.get("id") or voice.get("name")
    if voice is not None:
        return str(voice)
    return None


class OmniVoiceTextToSpeechConfig(OmniVoiceModelInfo, BaseTextToSpeechConfig):
    def get_supported_openai_params(self, model: str) -> list:
        return ["voice", "response_format", "speed", "language", "stream"]

    def map_openai_params(
        self,
        model: str,
        optional_params: dict,
        voice: Optional[str | dict] = None,
        drop_params: bool = False,
        kwargs: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[str], dict]:
        mapped_params: dict[str, Any] = {}

        resolved_voice = _resolve_voice(voice)

        for key in _TTS_FORM_KEYS:
            value = optional_params.pop(key, None)
            if value is not None:
                mapped_params[key] = value

        optional_params.pop("instructions", None)

        extra_body = optional_params.pop("extra_body", None)
        if isinstance(extra_body, dict):
            mapped_params.update({k: v for k, v in extra_body.items() if v is not None})

        if kwargs is not None:
            _collect_passthrough(kwargs, mapped_params)

        _collect_passthrough(optional_params, mapped_params)

        return resolved_voice, mapped_params

    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> dict:
        return headers

    def get_complete_url(
        self,
        model: str,
        api_base: Optional[str],
        litellm_params: dict,
    ) -> str:
        base = self._resolve_base(api_base)
        if base.lower().endswith("/v1"):
            base = base[:-3]
        return base + "/v1/audio/speech"

    def transform_text_to_speech_request(
        self,
        model: str,
        input: str,
        voice: Optional[str],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        request_body: dict[str, Any] = {
            "model": model,
            "input": input,
            "voice": voice or "alloy",
        }

        for key in _TTS_FORM_KEYS:
            value = optional_params.pop(key, None)
            if value is not None:
                request_body[key] = value

        _collect_passthrough(optional_params, request_body)

        return TextToSpeechRequestData(
            dict_body=request_body,
        )

    def transform_text_to_speech_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: Any,
    ) -> Any:
        from litellm.types.llms.openai import HttpxBinaryResponseContent

        return HttpxBinaryResponseContent(raw_response)
