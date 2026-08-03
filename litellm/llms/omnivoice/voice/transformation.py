from typing import Any, Optional

import httpx

from litellm.llms.base_llm.text_to_speech.transformation import (
    BaseTextToSpeechConfig,
    TextToSpeechRequestData,
)
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
        voice: Optional[str | dict] = None,
        drop_params: bool = False,
        kwargs: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[str], dict]:
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
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> dict:
        return headers

    def get_complete_url(
        self,
        model: str,
        api_base: Optional[str],
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
        voice: Optional[str],
        optional_params: dict,
        litellm_params: dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        ref_audio: Optional[FileTypes] = optional_params.pop("ref_audio", None)
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
