"""OpenAI official-SDK audio translation transport.

Plain OpenAI translation goes through the official SDK's
``audio.translations.with_raw_response.create`` (the same transport the
transcription handler uses), so OpenAI's own multipart encoding and auth
apply verbatim. Self-hosted native multipart servers (hosted-vLLM, NIM) do
NOT use this module: they ride the generic http handler with their own
configs, and never inherit this class.
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, Optional

import httpx
from openai import AsyncOpenAI, OpenAI
from pydantic import BaseModel

from litellm.litellm_core_utils.audio_utils.utils import get_audio_file_name
from litellm.litellm_core_utils.core_helpers import set_provider_response_headers_in_hidden_params
from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.types.llms.audio_transcription import NativeAudioDocumentResponse
from litellm.types.utils import FileTypes, TranscriptionResponse
from litellm.utils import convert_to_model_response_object, extract_duration_from_srt_or_vtt

from ..openai import OpenAIChatCompletion

if TYPE_CHECKING:
    from aiohttp import ClientSession

_SRT_VTT_MEDIA_TYPES: Final = frozenset({"text/plain", "application/x-subrip", "text/vtt"})


class OpenAIAudioTranslation(OpenAIChatCompletion):
    # Audio Translations
    async def make_openai_audio_translation_request(
        self,
        openai_aclient: AsyncOpenAI,
        data: dict,
        timeout: float | httpx.Timeout,
    ):
        raw_response = await openai_aclient.audio.translations.with_raw_response.create(**data, timeout=timeout)
        return raw_response

    def make_sync_openai_audio_translation_request(
        self,
        openai_client: OpenAI,
        data: dict,
        timeout: float | httpx.Timeout,
    ):
        raw_response = openai_client.audio.translations.with_raw_response.create(**data, timeout=timeout)
        return raw_response

    async def async_audio_translations(
        self,
        audio_file: FileTypes,
        data: dict,
        model_response: TranscriptionResponse,
        timeout: float,
        logging_obj: LiteLLMLoggingObj,
        api_key: str | None = None,
        api_base: str | None = None,
        client=None,
        max_retries=None,
        shared_session: Optional["ClientSession"] = None,
        litellm_params: Mapping[str, object] | None = None,
    ) -> TranscriptionResponse | NativeAudioDocumentResponse:
        try:
            openai_aclient: Final[AsyncOpenAI] = self._get_openai_client(
                is_async=True,
                api_key=api_key,
                api_base=api_base,
                timeout=timeout,
                max_retries=max_retries,
                client=client,
                shared_session=shared_session,
                litellm_params=litellm_params,
            )

            ## LOGGING
            logging_obj.pre_call(
                input=None,
                api_key=openai_aclient.api_key,
                additional_args={
                    "api_base": openai_aclient._base_url._uri_reference,  # pyright: ignore[reportPrivateUsage]  # SDK URL internals
                    "atranslation": True,
                    "complete_input_dict": data,
                },
            )
            raw_response = await self.make_openai_audio_translation_request(
                openai_aclient=openai_aclient,
                data=data,
                timeout=timeout,
            )
            headers: Final = dict(raw_response.headers)
            logging_obj.model_call_details["response_headers"] = headers
            parsed: Final = raw_response.parse()

            stringified_response: Final[dict[str, Any]] = _stringify_sdk_translation(parsed)
            ## LOGGING
            logging_obj.post_call(
                input=get_audio_file_name(audio_file),
                api_key=api_key,
                additional_args={"complete_input_dict": data},
                original_response=stringified_response,
            )
            actual_model: Final = data.get("model", "whisper-1")
            hidden_params: Final = {"model": actual_model, "custom_llm_provider": "openai"}

            if _is_document_media_type(headers):
                document: Final[NativeAudioDocumentResponse] = NativeAudioDocumentResponse(
                    body=str(getattr(parsed, "text", "") or ""),
                    media_type=_media_type(headers),
                    charset=None,
                    provider_response_headers={str(k): v for k, v in headers.items()},
                )
                document.hidden_params.update(hidden_params)
                set_provider_response_headers_in_hidden_params(document, headers)
                return document

            final_response: Final[TranscriptionResponse] = convert_to_model_response_object(
                response_object=stringified_response,
                model_response_object=model_response,
                hidden_params=hidden_params,
                response_type="audio_transcription",
            )
            set_provider_response_headers_in_hidden_params(final_response, headers)
            return final_response
        except Exception as e:
            ## LOGGING
            logging_obj.post_call(
                input=get_audio_file_name(audio_file),
                api_key=api_key,
                original_response=str(e),
            )
            raise e

    def audio_translations(
        self,
        audio_file: FileTypes,
        data: dict,
        model_response: TranscriptionResponse,
        timeout: float,
        logging_obj: LiteLLMLoggingObj,
        api_key: str | None = None,
        api_base: str | None = None,
        client=None,
        max_retries=None,
        shared_session: Optional["ClientSession"] = None,
        litellm_params: Mapping[str, object] | None = None,
        atranslation: bool = False,
    ) -> TranscriptionResponse | NativeAudioDocumentResponse:
        if atranslation is True:
            return self.async_audio_translations(
                audio_file=audio_file,
                data=data,
                model_response=model_response,
                timeout=timeout,
                logging_obj=logging_obj,
                api_key=api_key,
                api_base=api_base,
                client=client,
                max_retries=max_retries,
                shared_session=shared_session,
                litellm_params=litellm_params,
            )

        openai_client: Final[OpenAI] = self._get_openai_client(
            is_async=False,
            api_key=api_key,
            api_base=api_base,
            timeout=timeout,
            max_retries=max_retries,
            client=client,
            litellm_params=litellm_params,
        )

        ## LOGGING
        logging_obj.pre_call(
            input=None,
            api_key=openai_client.api_key,
            additional_args={
                "api_base": openai_client._base_url._uri_reference,  # pyright: ignore[reportPrivateUsage]  # SDK URL internals
                "atranslation": False,
                "complete_input_dict": data,
            },
        )

        raw_response: Final = self.make_sync_openai_audio_translation_request(
            openai_client=openai_client,
            data=data,
            timeout=timeout,
        )
        headers: Final = dict(raw_response.headers)
        parsed: Final = raw_response.parse()

        stringified_response: Final[dict[str, Any]] = _stringify_sdk_translation(parsed)
        ## LOGGING
        logging_obj.post_call(
            input=get_audio_file_name(audio_file),
            api_key=api_key,
            additional_args={"complete_input_dict": data},
            original_response=stringified_response,
        )
        actual_model: Final = data.get("model", "whisper-1")
        hidden_params: Final = {"model": actual_model, "custom_llm_provider": "openai"}

        if _is_document_media_type(headers):
            document: Final[NativeAudioDocumentResponse] = NativeAudioDocumentResponse(
                body=str(getattr(parsed, "text", "") or ""),
                media_type=_media_type(headers),
                charset=None,
                provider_response_headers={str(k): v for k, v in headers.items()},
            )
            document.hidden_params.update(hidden_params)
            set_provider_response_headers_in_hidden_params(document, headers)
            return document

        final_response: Final[TranscriptionResponse] = convert_to_model_response_object(
            response_object=stringified_response,
            model_response_object=model_response,
            hidden_params=hidden_params,
            response_type="audio_transcription",
        )
        set_provider_response_headers_in_hidden_params(final_response, headers)
        return final_response


def _media_type(headers: Mapping[str, str]) -> str:
    return next((v for k, v in headers.items() if k.lower() == "content-type"), "").split(";")[0].strip().lower()


def _is_document_media_type(headers: Mapping[str, str]) -> bool:
    return _media_type(headers) in _SRT_VTT_MEDIA_TYPES


def _stringify_sdk_translation(parsed: object) -> dict[str, Any]:
    """The SDK returns a typed model for JSON shapes or a plain ``str`` for
    text/SRT/VTT translations; both go through the same stringified form the
    transcription handler uses, so downstream conversion is one path."""
    if isinstance(parsed, BaseModel):
        return dict(parsed.model_dump())
    duration: Final = extract_duration_from_srt_or_vtt(str(parsed))
    stringified: Final[dict[str, Any]] = TranscriptionResponse(text=str(parsed)).model_dump()
    if duration is not None:
        stringified["_audio_transcription_duration"] = duration
    return stringified
