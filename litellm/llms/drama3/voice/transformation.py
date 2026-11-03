from typing import (
    Any,  # noqa: TID251  # matches the BaseVoiceConfig/TextToSpeechRequestData signatures siblings implement
)

import httpx

from litellm.litellm_core_utils.litellm_logging import (
    Logging as LiteLLMLoggingObj,
)
from litellm.llms.base_llm.text_to_speech.transformation import (
    TextToSpeechRequestData,
)
from litellm.llms.base_llm.voice.transformation import BaseVoiceConfig
from litellm.llms.drama3.common_utils import Drama3ModelInfo

_UPLOAD_FORM_KEYS: tuple[str, ...] = ("consent", "ref_text", "speaker_description")


def _resolve_upload_name(voice_data: dict[str, Any]) -> str:
    name = (
        voice_data.get("name")
        or voice_data.get("voice_id")
        or voice_data.get("speaker")
    )
    if name is None:
        from litellm.llms.base_llm.chat.transformation import BaseLLMException

        raise BaseLLMException(
            status_code=400,
            message="'name' is required for Drama 3 voice registration.",
            headers={},
        )
    return str(name)


def _resolve_upload_audio(
    voice_data: dict[str, Any],
    optional_params: dict[str, Any],
) -> dict[str, Any]:
    audio = (
        voice_data.get("ref_audio")
        or voice_data.get("audio_url")
        or voice_data.get("audio_path")
        or optional_params.get("ref_audio")
    )
    if audio is None:
        from litellm.llms.base_llm.chat.transformation import BaseLLMException

        raise BaseLLMException(
            status_code=400,
            message=(
                "'ref_audio' is required for Drama 3 voice registration. "
                "Provide the reference audio file."
            ),
            headers={},
        )
    if isinstance(audio, tuple):
        return {"audio_sample": audio}
    if isinstance(audio, (bytes, bytearray)):
        return {"audio_sample": ("ref_audio.wav", bytes(audio), "audio/wav")}
    from litellm.litellm_core_utils.audio_utils.utils import process_audio_file

    processed = process_audio_file(audio)
    return {
        "audio_sample": (
            processed.filename,
            processed.file_content,
            processed.content_type,
        )
    }


class Drama3VoiceConfig(Drama3ModelInfo, BaseVoiceConfig):
    """Voice management for the sglang-omni Drama 3 registry.

    The pod's own contract (sglang-omni ``serve/speech_voices.py``):

    - ``list``           -> GET    /v1/audio/voices
    - ``register``       -> POST   /v1/audio/voices (multipart)
    - ``delete_profile`` -> DELETE /v1/audio/voices/{name}

    Names are the pod's voice names, not OpenAI profile ids, but the
    ``delete_profile`` action is reused for deletes so the generic
    ``/v1/voices/profiles/{profile_id}`` route also works for this provider.
    """

    SUPPORTED_VOICE_ACTIONS: frozenset[str] = frozenset(
        {"register", "load", "list", "delete_profile"}
    )

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
        action = litellm_params.get("voice_action", "register")
        if action == "list":
            return base + "/v1/audio/voices"
        if action == "delete_profile":
            name = litellm_params.get("profile_id") or litellm_params.get("name")
            if name is None:
                from litellm.llms.base_llm.chat.transformation import BaseLLMException

                raise BaseLLMException(
                    status_code=400,
                    message="'name' (or profile_id) is required for Drama 3 voice deletion.",
                    headers={},
                )
            return base + "/v1/audio/voices/" + str(name)
        return base + "/v1/audio/voices"

    def transform_create_voice_request(
        self,
        model: str,
        voice_data: dict[str, Any],
        optional_params: dict[str, Any],
        litellm_params: dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        action = litellm_params.get("voice_action", "register")

        if action == "list":
            return TextToSpeechRequestData(method="GET")

        if action == "delete_profile":
            return TextToSpeechRequestData(method="DELETE")

        form_fields: dict[str, Any] = {"name": _resolve_upload_name(voice_data)}
        for key in _UPLOAD_FORM_KEYS:
            value = voice_data.get(key) or optional_params.get(key)
            if value is not None:
                form_fields[key] = value

        files = _resolve_upload_audio(voice_data, optional_params)
        return TextToSpeechRequestData(form_data=form_fields, files=files)

    def transform_create_voice_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
    ) -> dict[str, Any]:  # matches the BaseVoiceConfig signature siblings implement
        if not raw_response.content or raw_response.status_code == 204:
            return {"status": "success", "status_code": raw_response.status_code}
        return raw_response.json()
