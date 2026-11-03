import os

from litellm.llms.base_llm.base_utils import BaseLLMModelInfo
from litellm.types.utils import ProviderSpecificModelInfo

# Keys that must never reach the pod's request body but are NOT covered by
# is_litellm_owned_kwarg: the request-shape fields the transform builds itself
# (model/voice/input), the OpenAI-core fields it handles explicitly
# (response_format/speed), and prompt, which litellm does not own. Everything
# else litellm-owned is filtered via is_litellm_owned_kwarg, the canonical
# inventory in types/utils.py: the first cut used a hand-copied frozenset and
# the pod 400'd on allow_client_keepalive_override, a deployment litellm_params
# key the router merges into every call.
DRAMA3_INTERNAL_PARAMS: frozenset[str] = frozenset(
    {
        "additional_drop_params",
        "aspeech",
        "assistant",
        "async_mode",
        "base_url",
        "drop_params",
        "extra_body",
        "extra_headers",
        "input",
        "model",
        "original_function",
        "prompt",
        "provider_specific_params",
        "proxy_logging_obj",
        "response_format",
        "specific_deployment",
        "speed",
        "timeout",
        "timestamp_granularities",
        "user",
        "user_api_end_user_id",
        "user_api_key",
        "user_api_key_alias",
        "user_api_key_allowed_cache_controls",
        "user_api_key_max_budget",
        "user_api_key_models",
        "user_api_key_spend",
        "user_api_key_team_alias",
        "user_api_key_team_id",
        "user_api_key_team_max_budget",
        "user_api_key_team_spend",
        "user_api_key_user_id",
        "voice",
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
