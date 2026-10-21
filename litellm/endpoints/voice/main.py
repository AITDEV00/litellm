"""OICM custom voice/script SDK functions.

Self-contained vertical slice (VSA). Co-located under ``litellm/endpoints/voice/``
mirroring the upstream ``litellm/endpoints/speech/speech_to_completion_bridge``
shape. These are re-exported from ``litellm.main`` so the public ``litellm.*``
API (``litellm.acreate_voice``, ``litellm.create_voice``, ``litellm.ascript``,
``litellm.script``) keeps working.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Coroutine
from functools import partial
from typing import Any, Final, cast

import httpx
import openai

import litellm
from litellm import client
from litellm.litellm_core_utils.get_litellm_params import get_litellm_params
from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.types.llms.openai import HttpxBinaryResponseContent
from litellm.utils import ProviderConfigManager, exception_type, get_llm_provider


async def acreate_voice(*args, **kwargs) -> dict[str, Any]:
    loop = asyncio.get_event_loop()
    model = args[0] if len(args) > 0 else kwargs["model"]
    call_kwargs: Final = {**kwargs, "acreate_voice": True}
    try:
        func = partial(create_voice, *args, **call_kwargs)
        ctx = contextvars.copy_context()
        func_with_context = partial(ctx.run, func)

        response = await loop.run_in_executor(None, func_with_context)
        if asyncio.iscoroutine(response):
            response = await response
        return response
    except Exception as e:
        _, custom_llm_provider, _, _ = get_llm_provider(model=model, api_base=kwargs.get("api_base"))
        raise exception_type(
            model=model,
            custom_llm_provider=custom_llm_provider,
            original_exception=e,
            completion_kwargs=args,
            extra_kwargs=call_kwargs,
        ) from None


@client
def create_voice(
    model: str,
    voice_data: dict[str, Any],
    api_key: str | None = None,
    api_base: str | None = None,
    api_version: str | None = None,
    timeout: float | httpx.Timeout | None = None,
    max_retries: int | None = None,
    client: Any | dict | None = None,
    custom_llm_provider: str | None = None,
    extra_headers: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    acreate_voice: bool | None = None,
    **kwargs,
) -> dict[str, Any]:
    user = kwargs.get("user")
    litellm_call_id: str | None = kwargs.get("litellm_call_id")
    proxy_server_request = kwargs.get("proxy_server_request")
    resolved_extra_headers = extra_headers or kwargs.get("extra_headers")
    model_info = kwargs.get("model_info")
    metadata = kwargs.get("metadata")
    resolved_timeout: Final = litellm.request_timeout if timeout is None else timeout
    resolved_max_retries: Final = (
        (litellm.num_retries or openai.DEFAULT_MAX_RETRIES) if max_retries is None else max_retries
    )
    call_kwargs: Final = {key: value for key, value in kwargs.items() if key != "base_url"}

    base_llm_http_handler = BaseLLMHTTPHandler()

    _, resolved_custom_llm_provider, dynamic_api_key, dynamic_api_base = get_llm_provider(
        model=model,
        custom_llm_provider=custom_llm_provider,
        api_base=api_base or kwargs.get("base_url"),
    )

    litellm_params_dict = get_litellm_params(
        api_key=api_key or dynamic_api_key,
        api_base=api_base or dynamic_api_base,
        api_version=api_version,
        extra_headers=resolved_extra_headers,
        headers=headers,
        model=model,
        custom_llm_provider=resolved_custom_llm_provider,
        timeout=resolved_timeout,
        max_retries=resolved_max_retries,
        **call_kwargs,
    )

    litellm_params_dict["voice_action"] = voice_data.get("action", "register")
    if voice_data.get("profile_id") is not None:
        litellm_params_dict["profile_id"] = voice_data["profile_id"]

    voice_provider_config = ProviderConfigManager.get_provider_voice_config(
        provider=litellm.LlmProviders(resolved_custom_llm_provider),
    )

    if voice_provider_config is None:
        raise litellm.BadRequestError(
            message="Voice management is not supported for provider={}".format(resolved_custom_llm_provider),
            model=model,
            llm_provider=resolved_custom_llm_provider,
        )

    logging_obj: LiteLLMLoggingObj = kwargs.get("litellm_logging_obj")  # pyright: ignore[reportAny]  # kwargs-typed; Logging is the runtime type
    logging_obj.update_environment_variables(
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
            **call_kwargs,
        },
        custom_llm_provider=resolved_custom_llm_provider,
    )

    optional_params: dict[str, Any] = {}

    return base_llm_http_handler.voice_handler(
        model=model,
        voice_data=voice_data,
        voice_provider_config=voice_provider_config,
        optional_params=optional_params,
        litellm_params=litellm_params_dict,
        logging_obj=logging_obj,
        timeout=resolved_timeout,
        extra_headers=resolved_extra_headers,
        client=client,
        _is_async=acreate_voice or False,
    )


async def ascript(*args, **kwargs) -> HttpxBinaryResponseContent:
    loop = asyncio.get_event_loop()
    model = args[0] if len(args) > 0 else kwargs["model"]
    call_kwargs: Final = {**kwargs, "aspeech": True}
    try:
        func = partial(script, *args, **call_kwargs)
        ctx = contextvars.copy_context()
        func_with_context = partial(ctx.run, func)

        init_response = await loop.run_in_executor(None, func_with_context)
        if asyncio.iscoroutine(init_response):
            response = await init_response
        else:
            response = await loop.run_in_executor(None, func_with_context)
        return response
    except Exception as e:
        _, custom_llm_provider, _, _ = get_llm_provider(model=model, api_base=kwargs.get("api_base"))
        raise exception_type(
            model=model,
            custom_llm_provider=custom_llm_provider,
            original_exception=e,
            completion_kwargs=args,
            extra_kwargs=call_kwargs,
        ) from None


@client
def script(
    model: str,
    input: str = "",
    voice: str | dict | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    api_version: str | None = None,
    organization: str | None = None,
    project: str | None = None,
    max_retries: int | None = None,
    metadata: dict | None = None,
    timeout: float | httpx.Timeout | None = None,
    response_format: str | None = None,
    speed: int | None = None,
    instructions: str | None = None,
    client=None,
    headers: dict | None = None,
    custom_llm_provider: str | None = None,
    aspeech: bool | None = None,
    **kwargs,
) -> HttpxBinaryResponseContent | Coroutine[Any, Any, HttpxBinaryResponseContent]:
    user = kwargs.get("user")
    litellm_call_id: str | None = kwargs.get("litellm_call_id")
    proxy_server_request = kwargs.get("proxy_server_request")
    extra_headers = kwargs.get("extra_headers")
    model_info = kwargs.get("model_info")
    resolved_model, resolved_custom_llm_provider, dynamic_api_key, resolved_api_base = get_llm_provider(
        model=model, custom_llm_provider=custom_llm_provider, api_base=api_base
    )  # pyright: ignore[reportAny]  # get_llm_provider returns a broad tuple; narrowed by usage
    call_kwargs: Final = {key: value for key, value in kwargs.items() if key != "tags"}

    optional_params: dict[str, Any] = {}
    if response_format is not None:
        optional_params["response_format"] = response_format
    if speed is not None:
        optional_params["speed"] = speed
    if instructions is not None:
        optional_params["instructions"] = instructions

    resolved_timeout: Final = litellm.request_timeout if timeout is None else timeout
    litellm_params_dict = get_litellm_params(**call_kwargs)

    text_to_speech_provider_config = ProviderConfigManager.get_provider_script_config(
        provider=litellm.LlmProviders(resolved_custom_llm_provider),
    )
    if text_to_speech_provider_config is None:
        raise litellm.BadRequestError(
            message="Script synthesis is not supported for provider={}".format(resolved_custom_llm_provider),
            model=resolved_model,
            llm_provider=resolved_custom_llm_provider,
        )

    resolved_voice, resolved_optional_params = text_to_speech_provider_config.map_openai_params(
        model=resolved_model,
        optional_params=optional_params,
        voice=voice,
        drop_params=False,
        kwargs=call_kwargs,
    )

    logging_obj: LiteLLMLoggingObj = cast(LiteLLMLoggingObj, kwargs.get("litellm_logging_obj"))
    logging_obj.update_environment_variables(
        model=resolved_model,
        user=user,
        optional_params=resolved_optional_params,
        litellm_params={
            "litellm_call_id": litellm_call_id,
            "proxy_server_request": proxy_server_request,
            "model_info": model_info,
            "metadata": metadata,
            "preset_cache_key": None,
            "stream_response": {},
            **call_kwargs,
        },
        custom_llm_provider=resolved_custom_llm_provider,
    )

    if resolved_api_base is not None:
        litellm_params_dict["api_base"] = resolved_api_base
    if api_key is not None:
        litellm_params_dict["api_key"] = api_key

    base_llm_http_handler = BaseLLMHTTPHandler()

    return base_llm_http_handler.text_to_speech_handler(
        model=resolved_model,
        input=input,
        voice=resolved_voice,
        text_to_speech_provider_config=text_to_speech_provider_config,
        text_to_speech_optional_params=resolved_optional_params,
        custom_llm_provider=resolved_custom_llm_provider,
        litellm_params=litellm_params_dict,
        logging_obj=logging_obj,
        timeout=resolved_timeout,
        extra_headers=extra_headers,
        client=client,
        _is_async=aspeech or False,
    )
