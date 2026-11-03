from __future__ import annotations

from typing import Any  # noqa: TID251  # matches the BaseTextToSpeechConfig signature siblings implement

import httpx

from litellm.litellm_core_utils.litellm_logging import (
    Logging as LiteLLMLoggingObj,
)
from litellm.llms.base_llm.text_to_speech.transformation import (
    BaseTextToSpeechConfig,
    TextToSpeechRequestData,
)
from litellm.llms.drama3.common_utils import (
    DRAMA3_INTERNAL_PARAMS,
    Drama3ModelInfo,
)

# Fields the sglang-omni Drama 3 request schema accepts at the top level of
# /v1/audio/speech (sglang_omni/serve/protocol.py CreateSpeechRequest), beyond
# the OpenAI core. Everything else the pod rejects, so forwarding is bounded to
# these and to extra_body entries the caller explicitly sends.
_DRAMA3_TTS_FIELDS: tuple[str, ...] = (
    "references",
    "top_k",
    "top_p",
    "temperature",
    "max_new_tokens",
    "repetition_penalty",
    "stream",
    "stream_format",
    "speed",
    "response_format",
)


class Drama3TextToSpeechConfig(Drama3ModelInfo, BaseTextToSpeechConfig):
    def get_supported_openai_params(self, model: str) -> list:
        return [
            "voice",
            "response_format",
            "speed",
            *_DRAMA3_TTS_FIELDS,
        ]

    def map_openai_params(
        self,
        model: str,
        optional_params: dict,
        voice: str | None = None,
        drop_params: bool = False,
        kwargs: dict[str, Any] | None = None,
    ) -> tuple:
        mapped_params: dict[str, Any] = {}

        for key in _DRAMA3_TTS_FIELDS:
            value = optional_params.pop(key, None)
            if value is not None:
                mapped_params[key] = value

        # The pod rejects `instructions`; dropping it here keeps the request
        # valid rather than surfacing an upstream 400 for an OpenAI-ism.
        optional_params.pop("instructions", None)

        extra_body = optional_params.pop("extra_body", None)
        if isinstance(extra_body, dict):
            mapped_params.update(
                {k: v for k, v in extra_body.items() if v is not None}
            )

        if kwargs is not None:
            for key, value in kwargs.items():
                if value is None or key in DRAMA3_INTERNAL_PARAMS:
                    continue
                if key in mapped_params:
                    continue
                if key in ("extra_body", "extra_headers"):
                    continue
                mapped_params[key] = value

        for key, value in optional_params.items():
            if value is None or key in DRAMA3_INTERNAL_PARAMS:
                continue
            if key in mapped_params:
                continue
            mapped_params[key] = value

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
        return base + "/v1/audio/speech"

    def transform_text_to_speech_request(
        self,
        model: str,
        input: str,
        voice: str | None,
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        request_body: dict[str, Any] = {
            "model": model,
            "input": input,
            "voice": voice or "default",
        }

        for key in _DRAMA3_TTS_FIELDS:
            value = optional_params.pop(key, None)
            if value is not None:
                request_body[key] = value

        for key, value in optional_params.items():
            if value is None or key in DRAMA3_INTERNAL_PARAMS:
                continue
            if key in request_body:
                continue
            request_body[key] = value

        return TextToSpeechRequestData(
            dict_body=request_body,
        )

    def transform_text_to_speech_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> Any:  # noqa: ANN401  # HttpxBinaryResponseContent; deferred import matches every sibling config
        from litellm.types.llms.openai import HttpxBinaryResponseContent

        return HttpxBinaryResponseContent(raw_response)
