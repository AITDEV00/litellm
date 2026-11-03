import os

from litellm.llms.base_llm.base_utils import BaseLLMModelInfo
from litellm.types.utils import ProviderSpecificModelInfo

# Internal litellm keys that must never reach the pod's request body. Mirrors
# HAMSA_INTERNAL_PARAMS in litellm/llms/hamsa/common_utils.py, minus the keys
# that are real top-level fields of the pod's CreateSpeechRequest (language,
# temperature): copying those from hamsa silently dropped them again, the same
# defect this provider exists to fix.
DRAMA3_INTERNAL_PARAMS: frozenset[str] = frozenset(
    {
        "model",
        "voice",
        "input",
        "response_format",
        "speed",
        "instructions",
        "prompt",
        "timestamp_granularities",
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
        "custom_llm_provider",
        "client",
        "shared_session",
        "headers",
        "base_model",
        "base_url",
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
        "use_in_pass_through",
        "use_litellm_proxy",
        "use_xai_oauth",
        "use_chat_completions_api",
        "merge_reasoning_content_in_choices",
    }
)


class Drama3ModelInfo(BaseLLMModelInfo):
    def get_provider_info(self, model: str) -> ProviderSpecificModelInfo | None:
        return ProviderSpecificModelInfo(
            supports_audio_input=False,
            supports_audio_output=True,
        )

    def get_models(self, api_key: str | None = None, api_base: str | None = None) -> list[str]:
        return []

    @staticmethod
    def get_api_key(api_key: str | None = None) -> str | None:
        return api_key or os.environ.get("DRAMA3_API_KEY")

    @staticmethod
    def get_api_base(api_base: str | None = None) -> str | None:
        return api_base or os.environ.get("DRAMA3_API_BASE")

    @staticmethod
    def _resolve_base(api_base: str | None = None) -> str:
        base = Drama3ModelInfo.get_api_base(api_base)
        if base is None:
            from litellm.llms.base_llm.chat.transformation import BaseLLMException

            raise BaseLLMException(
                status_code=400,
                message="Missing Drama 3 API base. Set api_base in model config.",
                headers={},
            )
        return base.rstrip("/")

    @staticmethod
    def get_base_model(model: str) -> str | None:
        return model
