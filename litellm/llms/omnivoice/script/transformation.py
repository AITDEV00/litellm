from typing import Any

import httpx

from litellm.llms.base_llm.text_to_speech.transformation import (
    BaseTextToSpeechConfig,
    TextToSpeechRequestData,
)
from litellm.llms.omnivoice.common_utils import (
    OmniVoiceModelInfo,
    _collect_passthrough,
)


class OmniVoiceScriptConfig(OmniVoiceModelInfo, BaseTextToSpeechConfig):
    """Multi-speaker script synthesis: POST /v1/audio/script.

    Accepts a JSON body with ``script`` (list of speaker/text segments) and
    ``speakers`` (list of speaker/voice mappings), returns WAV audio.
    """

    def get_supported_openai_params(self, model: str) -> list:
        return ["voice", "response_format", "speed", "language", "stream"]

    def map_openai_params(
        self,
        model: str,
        optional_params: dict,
        voice: str | dict | None = None,
        drop_params: bool = False,
        kwargs: dict[str, Any] | None = None,
    ) -> tuple[str | None, dict]:
        mapped_params: dict[str, Any] = {}

        for key in ("response_format", "speed", "language", "stream"):
            value = optional_params.pop(key, None)
            if value is not None:
                mapped_params[key] = value

        optional_params.pop("instructions", None)

        extra_body = optional_params.pop("extra_body", None)
        if isinstance(extra_body, dict):
            mapped_params.update({k: v for k, v in extra_body.items() if v is not None})

        if kwargs is not None:
            script = kwargs.pop("script", None)
            if script is not None:
                mapped_params["script"] = script
            speakers = kwargs.pop("speakers", None)
            if speakers is not None:
                mapped_params["speakers"] = speakers
            _collect_passthrough(kwargs, mapped_params)

        _collect_passthrough(optional_params, mapped_params)

        return voice, mapped_params

    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> dict:
        return headers

    def get_complete_url(
        self,
        model: str,
        api_base: str | None,
        litellm_params: dict,
    ) -> str:
        base = self._resolve_base(api_base)
        if base.lower().endswith("/v1"):
            base = base[:-3]
        return base + "/v1/audio/script"

    def transform_text_to_speech_request(
        self,
        model: str,
        input: str,
        voice: str | None,
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        script = optional_params.pop("script", None)
        speakers = optional_params.pop("speakers", None)

        request_body: dict[str, Any] = {"model": model}

        if script is not None:
            request_body["script"] = script
        if speakers is not None:
            request_body["speakers"] = speakers

        for key in ("response_format", "speed", "language", "stream"):
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
