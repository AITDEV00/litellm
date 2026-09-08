import json
from typing import Any, Dict, Optional, Tuple

import httpx

from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.base_llm.text_to_speech.transformation import (
    BaseTextToSpeechConfig,
    TextToSpeechRequestData,
)
from litellm.llms.base_llm.voice.transformation import BaseVoiceConfig
from litellm.llms.hamsa.common_utils import (
    HAMSA_API_SURFACE_V1,
    HamsaModelInfo,
    resolve_api_surface,
    surface_path,
)


class HamsaVoiceConfig(HamsaModelInfo, BaseVoiceConfig):
    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> dict:
        return self._inject_auth_headers(headers, api_key)

    def get_complete_url(
        self,
        model: str,
        api_base: Optional[str],
        litellm_params: dict,
    ) -> str:
        base = self._resolve_base(api_base)
        action = litellm_params.get("voice_action", "register")
        if action == "load":
            return base + surface_path("voice_load", litellm_params)
        return base + surface_path("voice_clone", litellm_params)

    def transform_create_voice_request(
        self,
        model: str,
        voice_data: Dict[str, Any],
        optional_params: Dict,
        litellm_params: Dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        action = litellm_params.get("voice_action", "register")

        speaker = (
            voice_data.get("speaker")
            or voice_data.get("speaker_id")
            or voice_data.get("voice_id")
            or voice_data.get("name")
        )

        if action == "load":
            body: Dict[str, Any] = {
                "speaker_id": speaker,
            }
            global_token_ids = voice_data.get("global_token_ids")
            semantic_token_ids = voice_data.get("semantic_token_ids")
            if global_token_ids is None or semantic_token_ids is None:
                raise BaseLLMException(
                    status_code=400,
                    message="'global_token_ids' and 'semantic_token_ids' are required when action='load'. Provide the tokens returned from the initial voice_clone call.",
                    headers={},
                )
            body["global_token_ids"] = global_token_ids
            body["semantic_token_ids"] = semantic_token_ids
            prompt_text = voice_data.get("prompt_text")
            if prompt_text:
                body["prompt_text"] = prompt_text
            dialect = voice_data.get("dialect") or "msa"
            body["dialect"] = dialect
            return TextToSpeechRequestData(
                dict_body=body,
            )

        audio_url = voice_data.get("audio_url") or voice_data.get("audio")
        if audio_url is None:
            raise BaseLLMException(
                status_code=400,
                message="'audio_url' is required when action='register'. Provide a URL to the reference audio file.",
                headers={},
            )
        prompt_text = voice_data.get("prompt_text") or voice_data.get("transcript")
        if prompt_text is None:
            raise BaseLLMException(
                status_code=400,
                message="'prompt_text' is required when action='register'. Provide the transcript of the reference audio.",
                headers={},
            )

        body = {
            "audio_url": audio_url,
            "prompt_text": prompt_text,
        }
        if speaker:
            body["speaker"] = speaker

        return TextToSpeechRequestData(
            dict_body=body,
        )

    _SPEAKER_ALIASES: frozenset[str] = frozenset({"speaker", "voice_id", "speaker_id"})
    _AUDIO_PATH_ALIASES: frozenset[str] = frozenset({"audio_path", "path", "stored_path"})

    def transform_create_voice_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: Any,
    ) -> Dict[str, Any]:
        response_json = raw_response.json()

        if not isinstance(response_json, dict):
            return {"voice_id": "", "status": "registered"}

        speaker_name = next(
            (response_json[k] for k in self._SPEAKER_ALIASES if response_json.get(k)),
            None,
        )
        audio_path = next(
            (response_json[k] for k in self._AUDIO_PATH_ALIASES if response_json.get(k)),
            None,
        )

        result: Dict[str, Any] = {
            "voice_id": speaker_name or "",
            "status": response_json.get("status", "registered"),
        }
        if audio_path is not None:
            result["stored_path"] = audio_path

        consumed_keys = self._SPEAKER_ALIASES | self._AUDIO_PATH_ALIASES | {"status"}
        for key, value in response_json.items():
            if key not in consumed_keys:
                result[key] = value

        return result


class HamsaVoiceCloneConfig(HamsaModelInfo, BaseTextToSpeechConfig):
    """Config for the proxy's POST /v1/audio/speech/clone route on hamsa.

    Body shape depends on the pod's API surface:
    - native: multipart directly to /tts/voice_clone (ref_audio bytes go in
      ``files=``), matching the old two-step protocol.
    - v1 (tts-2026.09.08 pods): /v1/voice-clone accepts ONLY JSON
      {"audio_url", "prompt_text"} — the pod downloads the URL itself and
      there is no upload endpoint. An uploaded ref_audio cannot be forwarded,
      so callers must pass audio_url (form field) instead; uploading bytes
      yields an explicit 400 rather than the previous opaque serialization
      500.
    """

    def get_supported_openai_params(self, model: str) -> list:
        return ["voice", "response_format", "speed"]

    def map_openai_params(
        self,
        model: str,
        optional_params: Dict,
        voice: Optional[str] = None,
        drop_params: bool = False,
        kwargs: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[str], Dict]:
        mapped_params: Dict[str, Any] = {}
        if kwargs is not None:
            ref_audio = kwargs.get("ref_audio")
            if ref_audio is not None:
                mapped_params["ref_audio"] = ref_audio
            ref_text = kwargs.get("ref_text")
            if ref_text is not None:
                mapped_params["ref_text"] = ref_text
        return voice, mapped_params

    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> dict:
        return self._inject_auth_headers(headers, api_key)

    def get_complete_url(
        self,
        model: str,
        api_base: Optional[str],
        litellm_params: dict,
    ) -> str:
        base = self._resolve_base(api_base)
        surface = resolve_api_surface(litellm_params)
        if surface == HAMSA_API_SURFACE_V1:
            return base + "/v1/voice-clone"
        return base + "/tts/voice_clone"

    def transform_text_to_speech_request(
        self,
        model: str,
        input: str,
        voice: Optional[str],
        optional_params: Dict,
        litellm_params: Dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        surface = resolve_api_surface(litellm_params)

        ref_audio = optional_params.pop("ref_audio", None)
        ref_text = optional_params.pop("ref_text", None)

        if surface == HAMSA_API_SURFACE_V1:
            if ref_audio is not None:
                raise BaseLLMException(
                    status_code=400,
                    message=(
                        "Hamsa v1 pods cannot accept uploaded reference audio: "
                        "/v1/voice-clone downloads audio from 'audio_url' only. "
                        "Host the reference clip where the pod can reach it and "
                        "pass audio_url instead of ref_audio."
                    ),
                    headers={},
                )
            audio_url = litellm_params.get("audio_url") or (
                ref_text if isinstance(ref_text, str) and ref_text.startswith("http") else None
            )
            if audio_url is None:
                raise BaseLLMException(
                    status_code=400,
                    message="'audio_url' is required for voice cloning on Hamsa v1 pods. Provide a URL the pod can fetch.",
                    headers={},
                )
            body: Dict[str, Any] = {"audio_url": audio_url, "prompt_text": input}
            speaker = voice if voice and voice != "clone" else None
            if speaker:
                body["speaker_id"] = speaker
            return TextToSpeechRequestData(dict_body=body)

        # native surface: multipart upload
        if ref_audio is None:
            raise BaseLLMException(
                status_code=400,
                message="'ref_audio' is required for voice cloning. Provide a reference audio file.",
                headers={},
            )
        from litellm.litellm_core_utils.audio_utils.utils import process_audio_file

        processed = process_audio_file(ref_audio)
        form_fields: Dict[str, Any] = {"text": input}
        if ref_text is not None:
            form_fields["ref_text"] = ref_text
        return TextToSpeechRequestData(
            form_data=form_fields,
            files={"ref_audio": (processed.filename, processed.file_content, processed.content_type)},
        )

    def transform_text_to_speech_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: Any,
    ) -> Any:
        from litellm.types.llms.openai import HttpxBinaryResponseContent

        try:
            payload = raw_response.json()
        except (ValueError, json.JSONDecodeError):
            return HttpxBinaryResponseContent(raw_response)

        # v1 clone returns the token bundle, not audio. Surface it as the dict
        # so callers can feed action=load on /v1/audio/voices.
        if isinstance(payload, dict):
            return payload
        return HttpxBinaryResponseContent(raw_response)
