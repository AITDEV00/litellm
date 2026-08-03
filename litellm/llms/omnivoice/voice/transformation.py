from typing import Any

import httpx

from litellm.llms.base_llm.text_to_speech.transformation import (
    BaseTextToSpeechConfig,
    TextToSpeechRequestData,
)
from litellm.llms.base_llm.voice.transformation import BaseVoiceConfig
from litellm.llms.omnivoice.common_utils import (
    OmniVoiceModelInfo,
    _collect_passthrough,
)
from litellm.types.utils import FileTypes

_CLONE_FORM_KEYS: tuple[str, ...] = (
    "response_format",
    "speed",
    "stream",
    "num_step",
    "guidance_scale",
    "denoise",
    "t_shift",
    "position_temperature",
    "class_temperature",
    "duration",
    "language",
    "layer_penalty_factor",
    "preprocess_prompt",
    "postprocess_output",
    "audio_chunk_duration",
    "audio_chunk_threshold",
    "request_timeout_s",
)


class OmniVoiceVoiceCloneConfig(OmniVoiceModelInfo, BaseTextToSpeechConfig):
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

        for key in _CLONE_FORM_KEYS:
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
        return base + "/v1/audio/speech/clone"

    def transform_text_to_speech_request(
        self,
        model: str,
        input: str,
        voice: str | None,
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        ref_audio: FileTypes | None = optional_params.pop("ref_audio", None)
        if ref_audio is None:
            from litellm.llms.base_llm.chat.transformation import BaseLLMException

            raise BaseLLMException(
                status_code=400,
                message="'ref_audio' is required for voice cloning. Provide a reference audio file.",
                headers={},
            )

        ref_text = optional_params.pop("ref_text", None)

        form_fields: dict[str, Any] = {
            "text": input,
        }
        if ref_text is not None:
            form_fields["ref_text"] = ref_text

        for key in _CLONE_FORM_KEYS:
            value = optional_params.pop(key, None)
            if value is not None:
                form_fields[key] = value

        _collect_passthrough(optional_params, form_fields)

        if isinstance(ref_audio, tuple):
            files = {"ref_audio": ref_audio}
        elif isinstance(ref_audio, (bytes, bytearray)):
            files = {"ref_audio": ("ref_audio.wav", bytes(ref_audio), "audio/wav")}
        else:
            from litellm.litellm_core_utils.audio_utils.utils import process_audio_file

            processed = process_audio_file(ref_audio)
            files = {
                "ref_audio": (processed.filename, processed.file_content, processed.content_type),
            }

        return TextToSpeechRequestData(
            form_data=form_fields,
            files=files,
        )

    def transform_text_to_speech_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: Any,
    ) -> Any:
        from litellm.types.llms.openai import HttpxBinaryResponseContent

        return HttpxBinaryResponseContent(raw_response)


_PROFILE_FORM_KEYS: tuple[str, ...] = (
    "response_format",
    "speed",
    "stream",
    "num_step",
    "guidance_scale",
    "denoise",
    "t_shift",
    "position_temperature",
    "class_temperature",
    "duration",
    "language",
    "layer_penalty_factor",
    "preprocess_prompt",
    "postprocess_output",
    "audio_chunk_duration",
    "audio_chunk_threshold",
    "request_timeout_s",
    "ref_text",
)


class OmniVoiceVoiceConfig(OmniVoiceModelInfo, BaseVoiceConfig):
    """Voice management config for OmniVoice: list voices and profile CRUD.

    Uses ``voice_action`` in ``litellm_params`` to select the endpoint and HTTP method:
      - "list"            -> GET    /v1/voices
      - "list_profiles"   -> GET    /v1/voices (profiles appear as clone: entries)
      - "get_profile"     -> GET    /v1/voices/profiles/{profile_id}
      - "create_profile"  -> POST   /v1/voices/profiles (multipart with ref_audio)
      - "update_profile"  -> PATCH  /v1/voices/profiles/{profile_id} (multipart form-data)
      - "delete_profile"  -> DELETE /v1/voices/profiles/{profile_id}
    """

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
        action = litellm_params.get("voice_action", "list")
        if action in ("list", "list_profiles"):
            return base + "/v1/voices"
        if action == "get_profile":
            profile_id = litellm_params.get("profile_id")
            if profile_id is None:
                from litellm.llms.base_llm.chat.transformation import BaseLLMException

                raise BaseLLMException(
                    status_code=400,
                    message="'profile_id' is required for get_profile.",
                    headers={},
                )
            return base + "/v1/voices/profiles/" + str(profile_id)
        if action == "create_profile":
            return base + "/v1/voices/profiles"
        if action in ("update_profile", "delete_profile"):
            profile_id = litellm_params.get("profile_id")
            if profile_id is None:
                from litellm.llms.base_llm.chat.transformation import BaseLLMException

                raise BaseLLMException(
                    status_code=400,
                    message=f"'profile_id' is required for {action}.",
                    headers={},
                )
            return base + "/v1/voices/profiles/" + str(profile_id)
        return base + "/v1/voices"

    def transform_create_voice_request(
        self,
        model: str,
        voice_data: dict[str, Any],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        action = litellm_params.get("voice_action", "list")

        if action in ("list", "list_profiles", "get_profile", "delete_profile"):
            method = "DELETE" if action == "delete_profile" else "GET"
            return TextToSpeechRequestData(method=method)

        if action == "update_profile":
            form_fields: dict[str, Any] = {}
            for key in ("ref_text", "speaker", "name", "voice_id", "dialect", "prompt_text"):
                value = voice_data.get(key)
                if value is not None:
                    form_fields[key] = value
            return TextToSpeechRequestData(form_data=form_fields, method="PATCH")

        if action == "create_profile":
            ref_audio = voice_data.get("ref_audio") or optional_params.get("ref_audio")
            profile_id = voice_data.get("profile_id")
            if profile_id is None:
                from litellm.llms.base_llm.chat.transformation import BaseLLMException

                raise BaseLLMException(
                    status_code=400,
                    message="'profile_id' is required for voice profile creation.",
                    headers={},
                )

            form_fields: dict[str, Any] = {"profile_id": profile_id}

            for key in _PROFILE_FORM_KEYS:
                value = voice_data.pop(key, None) or optional_params.pop(key, None)
                if value is not None:
                    form_fields[key] = value

            _collect_passthrough(voice_data, form_fields)
            _collect_passthrough(optional_params, form_fields)

            files: dict[str, Any] = {}
            if ref_audio is not None:
                if isinstance(ref_audio, tuple):
                    files["ref_audio"] = ref_audio
                elif isinstance(ref_audio, (bytes, bytearray)):
                    files["ref_audio"] = ("ref_audio.wav", bytes(ref_audio), "audio/wav")
                else:
                    from litellm.litellm_core_utils.audio_utils.utils import process_audio_file

                    processed = process_audio_file(ref_audio)
                    files["ref_audio"] = (processed.filename, processed.file_content, processed.content_type)

            return TextToSpeechRequestData(form_data=form_fields, files=files)

        return TextToSpeechRequestData(method="GET")

    def transform_create_voice_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: Any,
    ) -> dict[str, Any]:
        if not raw_response.content or raw_response.status_code == 204:
            return {"status": "success", "status_code": raw_response.status_code}
        return raw_response.json()
