"""
Main document_conversion function for LiteLLM.
"""

from typing import Any, cast

import httpx

import litellm
from litellm._logging import verbose_logger
from litellm.constants import request_timeout
from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
from litellm.llms.base_llm.document_conversion.transformation import (
    BaseDocumentConversionConfig,
    ConvertDocumentResponse,
    DocumentConversionSource,
)
from litellm.llms.custom_httpx.llm_http_handler import BaseLLMHTTPHandler
from litellm.utils import ProviderConfigManager, client

####### ENVIRONMENT VARIABLES ###################
base_llm_http_handler = BaseLLMHTTPHandler()
#################################################

__all__ = ["aconvert", "convert"]


def _convert_sources(sources: list | dict | str) -> list[DocumentConversionSource]:
    """
    Normalize the user-supplied ``sources`` into a list of
    ``DocumentConversionSource``. Accepts a single source dict, a bare source
    URL/base64 string, or a list of either.
    """
    if isinstance(sources, str) or isinstance(sources, dict):
        sources = [sources]
    if not isinstance(sources, list):
        raise ValueError(f"sources must be a list, dict, or str; got {type(sources)}")

    normalized: list[DocumentConversionSource] = []
    for source in sources:
        if isinstance(source, DocumentConversionSource):
            normalized.append(source)
        elif isinstance(source, dict):
            normalized.append(DocumentConversionSource.model_validate(source))
        elif isinstance(source, str):
            normalized.append(DocumentConversionSource(content=source))
        else:
            raise ValueError(f"Invalid source: {source!r}")
    if not normalized:
        raise ValueError("sources must not be empty")
    return normalized


def _prepare_document_conversion_request(
    model: str,
    sources: list[DocumentConversionSource],
    api_key: str | None,
    api_base: str | None,
    timeout: float | httpx.Timeout | None,
    custom_llm_provider: str | None,
    extra_headers: dict[str, object] | None,
    kwargs: dict[str, object],
) -> tuple[
    dict[str, object],
    str,
    str | None,
    str | None,
    BaseDocumentConversionConfig,
    dict[str, object],
    float | httpx.Timeout,
    LiteLLMLoggingObj,
]:
    """
    Shared preparation for document conversion calls.

    Returns a tuple of (completion_kwargs, model, api_key, api_base,
    provider_config, optional_params, effective_timeout, logging_obj).
    """
    litellm_logging_obj = cast(LiteLLMLoggingObj, kwargs.pop("litellm_logging_obj"))
    litellm_call_id = cast(str | None, kwargs.get("litellm_call_id", None))

    (
        model,
        custom_llm_provider,
        dynamic_api_key,
        dynamic_api_base,
    ) = litellm.get_llm_provider(
        model=model,
        custom_llm_provider=custom_llm_provider,
        api_base=api_base,
        api_key=api_key,
    )
    if dynamic_api_key:
        api_key = dynamic_api_key
    if dynamic_api_base:
        api_base = dynamic_api_base

    provider_config = ProviderConfigManager.get_provider_document_conversion_config(
        model=model,
        provider=litellm.LlmProviders(custom_llm_provider),
    )
    if provider_config is None:
        raise ValueError(
            f"Document conversion is not supported for provider: {custom_llm_provider}"
        )

    verbose_logger.debug(
        f"document_conversion call - model: {model}, provider: {custom_llm_provider}"
    )

    supported_params = provider_config.get_supported_document_conversion_params(model=model)
    non_default_params: dict[str, Any] = {}
    for param in supported_params:
        if param in kwargs:
            non_default_params[param] = kwargs.pop(param)

    # Arbitrary query parameters captured by the proxy route are forwarded
    # to the provider so it can append them to the upstream URL generically.
    query_params = kwargs.pop("query_params", None)
    if query_params is not None:
        non_default_params["query_params"] = query_params

    optional_params = provider_config.map_document_conversion_params(
        non_default_params=non_default_params,
        optional_params={},
        model=model,
    )

    effective_timeout = timeout or request_timeout

    litellm_logging_obj.update_from_kwargs(
        kwargs=kwargs,
        model=model,
        optional_params=optional_params,
        litellm_params={
            "litellm_call_id": litellm_call_id,
            "api_base": api_base,
        },
        custom_llm_provider=custom_llm_provider,
    )

    completion_kwargs: dict[str, object] = {
        "model": model,
        "custom_llm_provider": custom_llm_provider,
    }

    return (
        completion_kwargs,
        model,
        api_key,
        api_base,
        provider_config,
        cast(dict[str, object], optional_params),
        effective_timeout,
        litellm_logging_obj,
    )


@client
async def aconvert(
    model: str,
    sources: list | dict | str,
    api_key: str | None = None,
    api_base: str | None = None,
    timeout: float | httpx.Timeout | None = None,
    custom_llm_provider: str | None = None,
    extra_headers: dict[str, object] | None = None,
    **kwargs: object,
) -> ConvertDocumentResponse:
    """
    Async document conversion function.

    Args:
        model: Model name (e.g. "docling/PP-DocLayoutV3")
        sources: One or more sources to convert. Each source is a dict
            ``{"content": "<url-or-data-uri>", "mime_type": "..."}``, a URL/
            base64 string, or a list of such.
        api_key: Optional API key
        api_base: Optional API base URL
        timeout: Optional timeout
        custom_llm_provider: Optional custom LLM provider
        extra_headers: Optional extra headers
        **kwargs: Additional params (e.g. ``to_formats=["markdown"]``)

    Returns:
        ConvertDocumentResponse
    """
    completion_kwargs: dict[str, object] = {
        "model": model,
        "custom_llm_provider": custom_llm_provider or "",
    }
    try:
        normalized_sources = _convert_sources(sources)
        (
            completion_kwargs,
            model,
            api_key,
            api_base,
            provider_config,
            optional_params,
            effective_timeout,
            litellm_logging_obj,
        ) = _prepare_document_conversion_request(
            model=model,
            sources=normalized_sources,
            api_key=api_key,
            api_base=api_base,
            timeout=timeout,
            custom_llm_provider=custom_llm_provider,
            extra_headers=extra_headers,
            kwargs=kwargs,
        )
        custom_llm_provider = cast(str, completion_kwargs.get("custom_llm_provider"))
        return await base_llm_http_handler.async_document_conversion(
            model=model,
            sources=normalized_sources,
            optional_params=optional_params,
            timeout=effective_timeout,
            logging_obj=litellm_logging_obj,
            api_key=api_key,
            api_base=api_base,
            custom_llm_provider=custom_llm_provider,
            headers=extra_headers,
            provider_config=provider_config,
        )
    except Exception as e:
        raise litellm.exception_type(
            model=model,
            custom_llm_provider=custom_llm_provider,
            original_exception=e,
            completion_kwargs=completion_kwargs,
            extra_kwargs=kwargs,
        )


@client
def convert(
    model: str,
    sources: list | dict | str,
    api_key: str | None = None,
    api_base: str | None = None,
    timeout: float | httpx.Timeout | None = None,
    custom_llm_provider: str | None = None,
    extra_headers: dict[str, object] | None = None,
    **kwargs: object,
) -> ConvertDocumentResponse:
    """
    Synchronous document conversion function.

    See :func:`aconvert` for parameter documentation.
    """
    import asyncio

    completion_kwargs: dict[str, object] = {
        "model": model,
        "custom_llm_provider": custom_llm_provider or "",
    }
    try:
        _is_async = kwargs.pop("aconvert", False) is True
        if _is_async:
            return asyncio.run(
                aconvert(
                    model=model,
                    sources=sources,
                    api_key=api_key,
                    api_base=api_base,
                    timeout=timeout,
                    custom_llm_provider=custom_llm_provider,
                    extra_headers=extra_headers,
                    **kwargs,
                )
            )
        normalized_sources = _convert_sources(sources)
        (
            completion_kwargs,
            model,
            api_key,
            api_base,
            provider_config,
            optional_params,
            effective_timeout,
            litellm_logging_obj,
        ) = _prepare_document_conversion_request(
            model=model,
            sources=normalized_sources,
            api_key=api_key,
            api_base=api_base,
            timeout=timeout,
            custom_llm_provider=custom_llm_provider,
            extra_headers=extra_headers,
            kwargs=kwargs,
        )
        custom_llm_provider = cast(str, completion_kwargs.get("custom_llm_provider"))
        return base_llm_http_handler.document_conversion(
            model=model,
            sources=normalized_sources,
            optional_params=optional_params,
            timeout=effective_timeout,
            logging_obj=litellm_logging_obj,
            api_key=api_key,
            api_base=api_base,
            custom_llm_provider=custom_llm_provider,
            headers=extra_headers,
            provider_config=provider_config,
        )
    except Exception as e:
        raise litellm.exception_type(
            model=model,
            custom_llm_provider=custom_llm_provider,
            original_exception=e,
            completion_kwargs=completion_kwargs,
            extra_kwargs=kwargs,
        )