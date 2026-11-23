"""OICM audio translation SDK functions.

Self-contained vertical slice (VSA), mirroring ``litellm/endpoints/diarization``
and the upstream transcription entrypoints in ``litellm/main.py``. Re-exported
so the public ``litellm.*`` API (``litellm.atranslation``, ``litellm.translation``)
keeps working through lazy import.

Translation is its own operation (the plan's D7): the public call type
``atranslation`` flows through ``function_setup`` (file checksum computed
before any API call, same as transcription), ``is_sync_litellm_request``
classification, cost selection keyed on ``CallTypes.atranslation.value``, and
the response hooks. OpenAI dispatches to the official SDK transport; native
self-hosted servers (hosted-vLLM, NIM) dispatch to the generic http handler
with their provider configs. NIM's ``target_language`` rides provider
extension kwargs to its config.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Coroutine
from functools import partial
from typing import Final, Literal, cast

import httpx
import openai

import litellm
from litellm import client
from litellm.litellm_core_utils.audio_utils.utils import calculate_request_duration
from litellm.litellm_core_utils.get_litellm_params import get_litellm_params
from litellm.litellm_core_utils.get_llm_provider_logic import get_llm_provider
from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.litellm_core_utils.llm_response_utils.transcriptions import hydrate_transcription_response
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.types.llms.audio_transcription import (
    NativeAudioDocumentResponse,
    NonStreamingNativeAudioResult,
)
from litellm.types.utils import FileTypes, LlmProviders, TranscriptionResponse
from litellm.utils import (
    ProviderConfigManager,
    exception_type,
    get_non_default_transcription_params,
    get_optional_params_translation,
)


@client
async def atranslation(*args, **kwargs) -> NonStreamingNativeAudioResult:
    """
    Async audio translation through the provider's /audio/translations endpoint.
    """
    loop: Final = asyncio.get_event_loop()
    model: Final = args[0] if len(args) > 0 else kwargs["model"]
    kwargs["atranslation"] = True
    file: Final = kwargs.get("file", None)
    try:
        func: Final = partial(translation, *args, **kwargs)
        ctx: Final = contextvars.copy_context()
        func_with_context: Final = partial(ctx.run, func)

        init_response: Final = await loop.run_in_executor(None, func_with_context)
        response: TranscriptionResponse | NativeAudioDocumentResponse | None
        if isinstance(init_response, dict):
            response = hydrate_transcription_response(init_response)
        elif isinstance(init_response, (TranscriptionResponse, NativeAudioDocumentResponse)):
            response = init_response
        elif asyncio.iscoroutine(init_response):
            response = await init_response
        else:
            response = await loop.run_in_executor(None, func_with_context)
        if not isinstance(response, (TranscriptionResponse, NativeAudioDocumentResponse)):
            raise ValueError(
                f"Invalid response from translation provider, expected TranscriptionResponse or NativeAudioDocumentResponse, but got {type(response)}"
            )

        if response is not None and not isinstance(response, Coroutine) and file is not None:
            existing_duration: Final = getattr(response, "duration", None)
            if existing_duration is None:
                calculated_duration: Final = calculate_request_duration(file)
                if calculated_duration is not None:
                    response.hidden_params["audio_transcription_duration"] = calculated_duration

        return response
    except Exception as e:
        custom_llm_provider: Final = cast(str, kwargs.get("custom_llm_provider") or "openai")
        raise exception_type(
            model=model,
            custom_llm_provider=custom_llm_provider,
            original_exception=e,
            completion_kwargs=args,
            extra_kwargs=kwargs,
        ) from None


@client
def translation(
    model: str,
    file: FileTypes,
    ## OPTIONAL OPENAI PARAMS ##
    prompt: str | None = None,
    response_format: Literal["json", "text", "srt", "verbose_json", "vtt"] | None = None,
    temperature: int | None = None,
    ## LITELLM PARAMS ##
    user: str | None = None,
    timeout=600,
    api_key: str | None = None,
    api_base: str | None = None,
    api_version: str | None = None,
    max_retries: int | None = None,
    custom_llm_provider=None,
    base_url: str | None = None,
    **kwargs,
) -> NonStreamingNativeAudioResult | Coroutine[object, object, NonStreamingNativeAudioResult]:
    """
    Translate an audio file through the provider's /audio/translations endpoint.

    Extension parameters (e.g. NIM's ``target_language``) flow through
    ``**kwargs`` into the provider's native mapping.
    """
    litellm_call_id: Final = kwargs.get("litellm_call_id", None)
    proxy_server_request: Final = kwargs.get("proxy_server_request", None)
    model_info: Final = kwargs.get("model_info", None)
    metadata: Final = kwargs.get("metadata", None)
    atranslation: Final = kwargs.pop("atranslation", False)
    litellm_logging_obj: Final[LiteLLMLoggingObj] = (
        cast(  # cast-ok: kwargs carries the wrapper-constructed logging object
            LiteLLMLoggingObj, kwargs.get("litellm_logging_obj")
        )
    )
    extra_headers: Final = kwargs.get("extra_headers", None)
    shared_session: Final = kwargs.get("shared_session", None)
    kwargs.pop("tags", [])
    non_default_params: Final = get_non_default_transcription_params(kwargs)

    supplied_client: Final = kwargs.pop("client", None)
    client: Final = supplied_client if isinstance(supplied_client, (openai.AsyncOpenAI, openai.OpenAI)) else None
    native_client: Final = (
        supplied_client
        if isinstance(supplied_client, (HTTPHandler, AsyncHTTPHandler, httpx.Client, httpx.AsyncClient))
        else None
    )

    if litellm_logging_obj:
        litellm_logging_obj.model_call_details["client"] = str(client)

    if max_retries is None:
        max_retries = openai.DEFAULT_MAX_RETRIES

    model_response: Final = litellm.utils.TranscriptionResponse()

    model, custom_llm_provider, dynamic_api_key, api_base = get_llm_provider(
        model=model,
        custom_llm_provider=custom_llm_provider,
        api_base=api_base or base_url,
        api_key=api_key,
    )

    api_key = dynamic_api_key if dynamic_api_key is not None else api_key

    optional_params: Final = get_optional_params_translation(
        model=model,
        custom_llm_provider=custom_llm_provider,
        prompt=prompt,
        response_format=response_format,
        temperature=temperature,
        **non_default_params,
    )

    litellm_params_dict: Final = get_litellm_params(
        **{
            **kwargs,
            "atranslation": atranslation,
        }
    )

    litellm_logging_obj.update_environment_variables(
        model=model,
        user=user,
        optional_params={},
        litellm_params={
            "litellm_call_id": litellm_call_id,
            "proxy_server_request": proxy_server_request,
            "model_info": model_info,
            "metadata": metadata,
            "preset_cache_key": None,
            "stream_response": {},
            **kwargs,
        },
        custom_llm_provider=custom_llm_provider,
    )

    response: NonStreamingNativeAudioResult | Coroutine[object, object, NonStreamingNativeAudioResult] | None = None

    if custom_llm_provider == "openai":
        from litellm.llms.openai.translation.handler import OpenAIAudioTranslation

        api_base = (
            api_base
            or litellm.api_base
            or litellm.utils.get_secret("OPENAI_BASE_URL")
            or litellm.utils.get_secret("OPENAI_API_BASE")
            or "https://api.openai.com/v1"
        )
        static_api_key: Final = (
            api_key or litellm.api_key or litellm.openai_key or litellm.utils.get_secret("OPENAI_API_KEY")
        )
        translation_data: Final[dict[str, object]] = {
            "model": model,
            "file": file,
            **optional_params,
        }
        if extra_headers is not None:
            translation_data["extra_headers"] = extra_headers
        response = OpenAIAudioTranslation().audio_translations(
            audio_file=file,
            data=dict(translation_data),
            model_response=model_response,
            atranslation=atranslation,
            timeout=timeout,
            logging_obj=litellm_logging_obj,
            max_retries=max_retries,
            api_base=api_base,
            api_key=static_api_key,
            client=client,
            litellm_params=litellm_params_dict,
            shared_session=shared_session,
        )
    else:
        provider_config: Final = ProviderConfigManager.get_provider_audio_translation_config(
            model=model,
            provider=LlmProviders(custom_llm_provider),
        )
        if provider_config is None:
            raise litellm.BadRequestError(
                message=f"Audio translation is not supported for provider={custom_llm_provider}",
                model=model,
                llm_provider=custom_llm_provider,
            )
        response = BaseLLMHTTPHandler().audio_translations(
            model=model,
            audio_file=file,
            optional_params=optional_params,
            litellm_params=litellm_params_dict,
            model_response=model_response,
            atranslation=atranslation,
            max_retries=max_retries,
            client=native_client,
            timeout=timeout,
            logging_obj=litellm_logging_obj,
            api_base=api_base,
            api_key=api_key,
            custom_llm_provider=custom_llm_provider,
            headers={"extra_headers": extra_headers} if extra_headers is not None else {},
            provider_config=provider_config,
            shared_session=shared_session,
        )

    # Store duration in _hidden_params for cost calculation without exposing it
    # in the response body (same contract as transcription).
    if response is not None and not isinstance(response, Coroutine):
        existing_duration: Final = getattr(response, "duration", None)
        if existing_duration is None:
            calculated_duration: Final = calculate_request_duration(file)
            if calculated_duration is not None:
                response_hidden_params: Final = response.hidden_params
                response_hidden_params["audio_transcription_duration"] = calculated_duration

    if response is None:
        raise ValueError("Unmapped provider passed in. Unable to get the response.")
    return response
