import os
from typing import List, Optional

from litellm.llms.base_llm.base_utils import BaseLLMModelInfo
from litellm.types.utils import ProviderSpecificModelInfo

HAMSA_INTERNAL_PARAMS: frozenset[str] = frozenset(
    {
        "model",
        "voice",
        "response_format",
        "speed",
        "instructions",
        "language",
        "prompt",
        "temperature",
        "timestamp_granularities",
        "extra_body",
        "extra_headers",
        "user",
        "api_key",
        "api_base",
        "api_version",
        "max_retries",
        "timeout",
        "stream",
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


class HamsaModelInfo(BaseLLMModelInfo):
    def get_provider_info(self, model: str) -> Optional[ProviderSpecificModelInfo]:
        return ProviderSpecificModelInfo(
            supports_audio_input=True,
            supports_audio_output=True,
        )

    def get_models(self, api_key: Optional[str] = None, api_base: Optional[str] = None) -> List[str]:
        return []

    @staticmethod
    def get_api_key(api_key: Optional[str] = None) -> Optional[str]:
        return api_key or os.environ.get("HAMSA_API_KEY")

    @staticmethod
    def get_api_base(api_base: Optional[str] = None) -> Optional[str]:
        return api_base or os.environ.get("HAMSA_API_BASE")

    @staticmethod
    def _resolve_base(api_base: Optional[str] = None) -> str:
        base = HamsaModelInfo.get_api_base(api_base)
        if base is None:
            from litellm.llms.base_llm.chat.transformation import BaseLLMException

            raise BaseLLMException(
                status_code=400,
                message="Missing Hamsa API base. Set HAMSA_API_BASE or pass api_base in model config.",
                headers={},
            )
        return base.rstrip("/")

    @staticmethod
    def _inject_auth_headers(
        headers: dict,
        api_key: Optional[str] = None,
    ) -> dict:
        resolved_key = HamsaModelInfo.get_api_key(api_key)
        if resolved_key is None:
            from litellm.llms.base_llm.chat.transformation import BaseLLMException

            raise BaseLLMException(
                status_code=401,
                message="Missing Hamsa API key. Set HAMSA_API_KEY or pass api_key in model config.",
                headers={},
            )
        headers["x-api-key"] = resolved_key
        headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def get_base_model(model: str) -> Optional[str]:
        return model
