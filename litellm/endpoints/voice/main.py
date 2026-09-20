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
from typing import Any, cast

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
    kwargs["acreate_voice"] = True
    try:
        func = partial(create_voice, *args, **kwargs)
        ctx = contextvars.copy_context()
        func_with_context = partial(ctx.run, func)

        response = await loop.run_in_executor(None, func_with_context)
        if asyncio.iscoroutine(response):
            response = await response
        return response
    except Exception as e:
        _, custom_llm_provider, _, _ = get_llm_provider(model=model, api_base=kwargs.get("api_base", None))
        raise exception_type(
            model=model,
            custom_llm_provider=custom_llm_provider,
            original_exception=e,
            completion_kwargs=args,
            extra_kwargs=kwargs,
        )


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
    user = kwargs.get("user", None)
    litellm_call_id: str | None = kwargs.get("litellm_call_id", None)
    proxy_server_request = kwargs.get("proxy_server_request", None)
    extra_headers = kwargs.get("extra_headers", None)
    model_info = kwargs.get("model_info", None)
    metadata = kwargs.get("metadata", None)

    if timeout is None:
        timeout = litellm.request_timeout

    if max_retries is None:
        max_retries = litellm.num_retries or openai.DEFAULT_MAX_RETRIES

    base_llm_http_handler = BaseLLMHTTPHandler()

    _, custom_llm_provider, dynamic_api_key, dynamic_api_base = get_llm_provider(
        model=model,
        custom_llm_provider=custom_llm_provider,
        api_base=api_base or kwargs.pop("base_url", None),
    )

    litellm_params_dict = get_litellm_params(
        api_key=api_key or dynamic_api_key,
        api_base=api_base or dynamic_api_base,
        api_version=api_version,
        extra_headers=extra_headers,
        headers=headers,
        model=model,
        custom_llm_provider=custom_llm_provider,
        timeout=timeout,
        max_retries=max_retries,
        **kwargs,
    )

    litellm_params_dict["voice_action"] = voice_data.get("action", "register")
    if voice_data.get("profile_id") is not None:
        litellm_params_dict["profile_id"] = voice_data["profile_id"]

    voice_provider_config = ProviderConfigManager.get_provider_voice_config(
        provider=litellm.LlmProviders(custom_llm_provider),
    )

    if voice_provider_config is None:
        raise litellm.BadRequestError(
            message="Voice management is not supported for provider={}".format(custom_llm_provider),
            model=model,
            llm_provider=custom_llm_provider,
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
            **kwargs,
        },
        custom_llm_provider=custom_llm_provider,
    )

    optional_params: dict[str, Any] = {}

    response = base_llm_http_handler.voice_handler(
        model=model,
        voice_data=voice_data,
        voice_provider_config=voice_provider_config,
        optional_params=optional_params,
        litellm_params=litellm_params_dict,
        logging_obj=logging_obj,
        timeout=timeout,
        extra_headers=extra_headers,
        client=client,
        _is_async=acreate_voice or False,
    )
    return response


async def ascript(*args, **kwargs) -> HttpxBinaryResponseContent:
    loop = asyncio.get_event_loop()
    model = args[0] if len(args) > 0 else kwargs["model"]
    kwargs["aspeech"] = True
    try:
        func = partial(script, *args, **kwargs)
        ctx = contextvars.copy_context()
        func_with_context = partial(ctx.run, func)

        init_response = await loop.run_in_executor(None, func_with_context)
        if asyncio.iscoroutine(init_response):
            response = await init_response
        else:
            response = await loop.run_in_executor(None, func_with_context)
        return response
    except Exception as e:
        _, custom_llm_provider, _, _ = get_llm_provider(model=model, api_base=kwargs.get("api_base", None))
        raise exception_type(
            model=model,
            custom_llm_provider=custom_llm_provider,
            original_exception=e,
            completion_kwargs=args,
            extra_kwargs=kwargs,
        )


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
    user = kwargs.get("user", None)
    litellm_call_id: str | None = kwargs.get("litellm_call_id", None)
    proxy_server_request = kwargs.get("proxy_server_request", None)
    extra_headers = kwargs.get("extra_headers", None)
    model_info = kwargs.get("model_info", None)
    model, custom_llm_provider, dynamic_api_key, api_base = get_llm_provider(
        model=model, custom_llm_provider=custom_llm_provider, api_base=api_base
    )  # pyright: ignore[reportAny]  # get_llm_provider returns a broad tuple; narrowed by usage
    kwargs.pop("tags", [])

    optional_params: dict[str, Any] = {}
    if response_format is not None:
        optional_params["response_format"] = response_format
    if speed is not None:
        optional_params["speed"] = speed
    if instructions is not None:
        optional_params["instructions"] = instructions

    if timeout is None:
        timeout = litellm.request_timeout

    if max_retries is None:
        max_retries = litellm.num_retries or openai.DEFAULT_MAX_RETRIES
    litellm_params_dict = get_litellm_params(**kwargs)

    text_to_speech_provider_config = ProviderConfigManager.get_provider_script_config(
        provider=litellm.LlmProviders(custom_llm_provider),
    )
    if text_to_speech_provider_config is None:
        raise litellm.BadRequestError(
            message="Script synthesis is not supported for provider={}".format(custom_llm_provider),
            model=model,
            llm_provider=custom_llm_provider,
        )

    voice, optional_params = text_to_speech_provider_config.map_openai_params(
        model=model,
        optional_params=optional_params,
        voice=voice,
        drop_params=False,
        kwargs=kwargs,
    )

    logging_obj: LiteLLMLoggingObj = cast(LiteLLMLoggingObj, kwargs.get("litellm_logging_obj"))
    logging_obj.update_environment_variables(
        model=model,
        user=user,
        optional_params=optional_params,
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

    if api_base is not None:
        litellm_params_dict["api_base"] = api_base
    if api_key is not None:
        litellm_params_dict["api_key"] = api_key

    base_llm_http_handler = BaseLLMHTTPHandler()

    response = base_llm_http_handler.text_to_speech_handler(
        model=model,
        input=input,
        voice=voice,
        text_to_speech_provider_config=text_to_speech_provider_config,
        text_to_speech_optional_params=optional_params,
        custom_llm_provider=custom_llm_provider,
        litellm_params=litellm_params_dict,
        logging_obj=logging_obj,
        timeout=timeout,
        extra_headers=extra_headers,
        client=client,
        _is_async=aspeech or False,
    )
    return response
