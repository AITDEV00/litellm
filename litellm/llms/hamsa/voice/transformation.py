from typing import Any, Dict, Optional

import httpx

from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.base_llm.text_to_speech.transformation import (
    TextToSpeechRequestData,
)
from litellm.llms.base_llm.voice.transformation import BaseVoiceConfig
from litellm.llms.hamsa.common_utils import HamsaModelInfo


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
            return base + "/tts/load_voice_cloning"
        return base + "/tts/voice_clone"

    def transform_create_voice_request(
        self,
        model: str,
        voice_data: Dict[str, Any],
        optional_params: Dict,
        litellm_params: Dict,
        headers: dict,
    ) -> TextToSpeechRequestData:
        action = litellm_params.get("voice_action", "register")

        speaker = voice_data.get("speaker") or voice_data.get("speaker_id") or voice_data.get("voice_id") or voice_data.get("name")

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
