"""OICM custom diarization SDK functions.

Self-contained vertical slice (VSA). Co-located under
``litellm/endpoints/diarization/`` mirroring the ``litellm/endpoints/voice/``
shape. These are re-exported so the public ``litellm.*`` API (``litellm.adiarize``,
``litellm.diarize``) keeps working.

Diarization follows the pyannoteAI cloud ``POST /v1/diarize`` contract: multipart
audio file plus form fields forwarded verbatim, ``DiarizationJob`` JSON returned
as-is. The serving model id on ADEO coro deployments is ``pyannote/community-1``;
the pod rejects the spec enum default there, so the model field is always sent.
"""

from __future__ import annotations

import asyncio
import contextvars
from functools import partial
from typing import Any

import httpx
import openai

import litellm
from litellm import client
from litellm.litellm_core_utils.get_litellm_params import get_litellm_params
from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.utils import ProviderConfigManager, exception_type, get_llm_provider


async def adiarize(*args, **kwargs) -> dict[str, Any]:
    loop = asyncio.get_event_loop()
    model = args[0] if len(args) > 0 else kwargs["model"]
    kwargs["adiarize"] = True
    try:
        func = partial(diarize, *args, **kwargs)
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
            extra_kwargs=kwargs,
        ) from None


@client
def diarize(
    model: str,
    form_fields: dict[str, Any],
    audio_file: tuple,
    api_key: str | None = None,
    api_base: str | None = None,
    timeout: float | httpx.Timeout | None = None,
    max_retries: int | None = None,
    client: Any | dict | None = None,
    custom_llm_provider: str | None = None,
    extra_headers: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    adiarize: bool | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Diarize audio via the pyannoteAI ``POST /v1/diarize`` contract.

    Args:
        model: serving model id, e.g. ``pyannote/community-1``
        form_fields: request form fields forwarded verbatim to the pod
        audio_file: ``(filename, bytes, content_type)`` tuple
    """
    user = kwargs.get("user")
    litellm_call_id: str | None = kwargs.get("litellm_call_id")
    proxy_server_request = kwargs.get("proxy_server_request")
    resolved_extra_headers = extra_headers or kwargs.get("extra_headers")
    model_info = kwargs.get("model_info")
    metadata = kwargs.get("metadata")

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
        extra_headers=resolved_extra_headers,
        headers=headers,
        model=model,
        custom_llm_provider=custom_llm_provider,
        timeout=timeout,
        max_retries=max_retries,
        **kwargs,
    )

    diarization_provider_config = ProviderConfigManager.get_provider_diarization_config(
        provider=litellm.LlmProviders(custom_llm_provider),
    )

    if diarization_provider_config is None:
        raise litellm.BadRequestError(
            message="Diarization is not supported for provider={}".format(custom_llm_provider),
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

    # The pod's accepted serving id is the full pre-split string (e.g.
    # "pyannote/community-1"); the provider split above resolved dispatch only.
    return base_llm_http_handler.diarization_handler(
        model=model,
        form_fields=form_fields,
        audio_file=audio_file,
        diarization_provider_config=diarization_provider_config,
        optional_params=optional_params,
        litellm_params=litellm_params_dict,
        logging_obj=logging_obj,
        timeout=timeout,
        extra_headers=resolved_extra_headers,
        client=client,
        _is_async=adiarize or False,
    )
