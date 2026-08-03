from typing import Optional

from litellm.llms.base_llm.base_utils import BaseLLMModelInfo
from litellm.types.utils import ProviderSpecificModelInfo

INCEPTION_INTERNAL_PARAMS: frozenset[str] = frozenset(
    {
        "extra_body",
        "extra_headers",
        "user",
        "api_key",
        "api_base",
        "api_version",
        "max_retries",
        "timeout",
        "litellm_call_id",
        "litellm_logging_obj",
        "proxy_server_request",
        "proxy_logging_obj",
        "model_info",
        "metadata",
        "preset_cache_key",
        "cache",
        "provider_specific_params",
        "additional_drop_params",
        "drop_params",
        "aspeech",
        "atranscription",
        "custom_llm_provider",
        "client",
        "shared_session",
        "headers",
        "base_model",
        "base_url",
        "OPENAI_TRANSCRIPTION_PARAMS",
        "tags",
        "original_function",
        "specific_deployment",
        "user_api_key",
        "user_api_key_user_id",
        "user_api_key_team_id",
        "user_api_key_alias",
        "user_api_key_team_alias",
        "user_api_end_user_id",
        "user_api_key_team_max_budget",
        "user_api_key_team_spend",
        "user_api_key_spend",
        "user_api_key_max_budget",
        "user_api_key_models",
        "user_api_key_allowed_cache_controls",
        "request_timeout",
        "assistant",
        "async_mode",
        "litellm_session_id",
        "litellm_trace_id",
    }
)


class InceptionAudioModelInfo(BaseLLMModelInfo):
    def get_provider_info(self, model: str) -> Optional[ProviderSpecificModelInfo]:
        return ProviderSpecificModelInfo(
            supports_audio_input=True,
            supports_audio_output=True,
        )

    def get_models(self, api_key: Optional[str] = None, api_base: Optional[str] = None) -> list[str]:
        return []

    @staticmethod
    def get_api_key(api_key: Optional[str] = None) -> Optional[str]:
        return api_key or "no-api-key-required"

    @staticmethod
    def get_api_base(api_base: Optional[str] = None) -> Optional[str]:
        return api_base

    @staticmethod
    def _resolve_base(api_base: Optional[str] = None) -> str:
        base = InceptionAudioModelInfo.get_api_base(api_base)
        if base is None:
            from litellm.llms.base_llm.chat.transformation import BaseLLMException

            raise BaseLLMException(
                status_code=400,
                message="Missing Inception API base. Set api_base in model config.",
                headers={},
            )
        return base.rstrip("/")

    @staticmethod
    def get_base_model(model: str) -> Optional[str]:
        return model
