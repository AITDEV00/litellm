"""OpenAI translation transformation: URL and supported parameters.

Plain OpenAI's translation endpoint is ``/v1/audio/translations``. The
transport is the official SDK, so this config only owns the URL and the
standard OpenAI translation parameters; ``target_language`` is not an OpenAI
parameter and stays with the NIM/native configs.
"""

from typing import Final

from litellm.llms.base_llm.audio_translation.transformation import BaseAudioTranslationConfig
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.openai import AllMessageValues


class OpenAIError(BaseLLMException):
    def __init__(self, status_code: int, message: str, headers: dict | None = None):
        super().__init__(status_code=status_code, message=message, headers=headers)


class OpenAIWhisperAudioTranslationConfig(BaseAudioTranslationConfig):
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

    def get_supported_openai_params(self, model: str) -> list[str]:
        return [
            "prompt",
            "response_format",
            "temperature",
        ]

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
        api_key = api_key or get_secret_str("OPENAI_API_KEY")
        if api_key:
            headers.update({"Authorization": f"Bearer {api_key}"})
        return headers

    def get_error_class(self, error_message: str, status_code: int, headers: dict) -> BaseLLMException:
        return OpenAIError(
            status_code=status_code,
            message=error_message,
            headers=headers,
        )

    def transform_audio_translation_request(
        self,
        model: str,
        audio_file,
        optional_params: dict,
        litellm_params: dict,
    ):
        from litellm.llms.base_llm.audio_translation.transformation import AudioTranslationRequestData

        data: Final[dict[str, object]] = {"model": model, "file": audio_file, **optional_params}
        if "response_format" not in data:
            data["response_format"] = "verbose_json"
        return AudioTranslationRequestData(
            data=dict(data),
        )

    def transform_audio_translation_response(self, raw_response):
        raise NotImplementedError("OpenAI translations use the official SDK transport, not the generic http handler")
