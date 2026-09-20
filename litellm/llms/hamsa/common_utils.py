import os
from typing import List, Optional

from litellm.llms.base_llm.base_utils import BaseLLMModelInfo
from litellm.types.utils import ProviderSpecificModelInfo

# API surface variants deployed in the cluster:
# - "native": original build. Paths /tts/stream, /transcribe, and the
#   two-step /tts/voice_clone + /tts/load_voice_cloning protocol. Requires an
#   encrypted x-api-key header (Fernet auth.py inside the pod).
# - "v1": tts-2026.09.08 build. OpenAI-flavored paths /v1/speech, /v1/voices,
#   one-shot multipart /v1/voice-clone. No auth dependency at all.
# Selected per deployment via litellm_params["api_surface"] (stamped by the
# discovery controller) or the HAMSA_API_SURFACE env var; default "native".
HAMSA_API_SURFACE_NATIVE: str = "native"
HAMSA_API_SURFACE_V1: str = "v1"

# Path suffixes per surface, keyed by capability.
HAMSA_SURFACE_PATHS: dict[str, dict[str, str]] = {
    HAMSA_API_SURFACE_NATIVE: {
        "speech": "/tts/stream",
        "transcription": "/transcribe",
        "voice_clone": "/tts/voice_clone",
        "voice_load": "/tts/load_voice_cloning",
    },
    HAMSA_API_SURFACE_V1: {
        "speech": "/v1/speech",
        "transcription": "/transcribe",
        "voice_clone": "/v1/voice-clone",
        "voice_load": "/v1/voices",
    },
}


def resolve_api_surface(litellm_params: Optional[dict] = None) -> str:
    """Resolve the hamsa API surface: litellm_params override env override default."""
    from_params = (litellm_params or {}).get("api_surface")
    if isinstance(from_params, str) and from_params:
        surface = from_params
    else:
        surface = os.environ.get("HAMSA_API_SURFACE") or HAMSA_API_SURFACE_NATIVE
    if surface not in HAMSA_SURFACE_PATHS:
        raise ValueError(
            f"Unknown Hamsa API surface '{surface}'. "
            f"Expected one of {sorted(HAMSA_SURFACE_PATHS)}."
        )
    return surface


def surface_path(capability: str, litellm_params: Optional[dict] = None) -> str:
    """Path suffix for a capability (speech/transcription/voice_clone/voice_load)."""
    return HAMSA_SURFACE_PATHS[resolve_api_surface(litellm_params)][capability]

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
        "use_in_pass_through",
        "use_litellm_proxy",
        "use_xai_oauth",
        "use_chat_completions_api",
        "merge_reasoning_content_in_choices",
        # Surface selector: routing metadata, never a pod request field.
        "api_surface",
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
        # Keyless deployments (e.g. the v1-surface tts-2026.09.08 pods) have no
        # auth dependency; an empty/absent key means no header rather than an
        # error. Only raise when the caller explicitly configured a key
        # mechanism and it resolved to nothing via HAMSA_API_KEY.
        resolved_key = HamsaModelInfo.get_api_key(api_key)
        headers["Content-Type"] = "application/json"
        if resolved_key is None or str(resolved_key).strip() == "":
            return headers
        headers["x-api-key"] = resolved_key
        return headers

    @staticmethod
    def get_base_model(model: str) -> Optional[str]:
        return model
