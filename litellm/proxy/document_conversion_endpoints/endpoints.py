#### Document Conversion Endpoints #####

from typing import Any

import orjson
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import ORJSONResponse

from litellm._logging import verbose_proxy_logger
from litellm.llms.base_llm.document_conversion.transformation import (
    ConvertDocumentResponse,
)
from litellm.proxy._types import *
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing

router = APIRouter()


async def _parse_document_conversion_request(request: Request) -> dict[str, Any]:
    """
    Parse a document conversion request body (JSON only).

    The canonical /v1/convert/source shape is:
        {
            "sources": [{"content": "<url|data-uri>", "mimeType": "..."}],
            "options": {"from_formats": [], "to_formats": []}
        }
    """
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type.lower():
        raise ValueError(
            "Multipart form data is not supported for /v1/convert/source. "
            "Send a JSON body with a 'sources' array instead."
        )

    try:
        body = await request.body()
    except RuntimeError:
        # Body stream was consumed by auth middleware (e.g., form parsing).
        body = b""

    if not body:
        raise ValueError("Empty request body. Expected a JSON body with a 'sources' array.")

    try:
        data = orjson.loads(body)
    except orjson.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in request body: {e}.")

    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")

    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Request body must include a non-empty 'sources' array.")

    # Capture arbitrary incoming query parameters (e.g. ``debug=1``) so they
    # are forwarded to the upstream conversion service as-is. Nothing is
    # hardcoded; any query parameter on the route is passed through.
    data["query_params"] = dict(request.query_params)

    verbose_proxy_logger.debug(
        f"Document conversion request parsed - sources: {len(sources)}, "
        f"options: {data.get('options')}, query_params: {data.get('query_params')}"
    )

    return data


@router.post(
    "/v1/convert/source",
    dependencies=[Depends(user_api_key_auth)],
    response_class=ORJSONResponse,
    tags=["document_conversion"],
)
@router.post(
    "/convert/source",
    dependencies=[Depends(user_api_key_auth)],
    response_class=ORJSONResponse,
    tags=["document_conversion"],
)
async def document_conversion(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    Document conversion endpoint (canonical Docling-compatible shape).

    Accepts a JSON body with a 'sources' array and optional 'options':

    ```bash
    curl -X POST "http://localhost:4000/v1/convert/source" \
        -H "Authorization: Bearer sk-1234" \
        -H "Content-Type: application/json" \
        -d '{
            "model": "docling/PP-DocLayoutV3",
            "sources": [
                {"content": "https://example.com/doc.pdf", "mimeType": "application/pdf"}
            ],
            "options": {"to_formats": ["json"]}
        }'
    ```
    """
    from litellm.proxy.proxy_server import (
        general_settings,
        llm_router,
        proxy_config,
        proxy_logging_obj,
        select_data_generator,
        user_api_base,
        user_max_tokens,
        user_model,
        user_request_timeout,
        user_temperature,
        version,
    )

    data: dict = {}
    try:
        # Parse request body (JSON only)
        data = await _parse_document_conversion_request(request)

        # Process request using ProxyBaseLLMRequestProcessing
        processor = ProxyBaseLLMRequestProcessing(data=data)

        response = await processor.base_process_llm_request(
            request=request,
            fastapi_response=fastapi_response,
            user_api_key_dict=user_api_key_dict,
            route_type="aconvert",
            proxy_logging_obj=proxy_logging_obj,
            llm_router=llm_router,
            general_settings=general_settings,
            proxy_config=proxy_config,
            select_data_generator=select_data_generator,
            model=None,
            user_model=user_model,
            user_temperature=user_temperature,
            user_request_timeout=user_request_timeout,
            user_max_tokens=user_max_tokens,
            user_api_base=user_api_base,
            version=version,
        )

        # Docling returns a binary payload (image/png layout debug render)
        # when a ``debug`` query param is passed. Surface those raw bytes
        # verbatim with the upstream content type, mirroring how the video
        # endpoints return raw media bytes.
        raw_bytes = (
            response.get_raw_response_payload()
            if isinstance(response, ConvertDocumentResponse)
            else None
        )
        if isinstance(raw_bytes, bytes):
            content_type = (
                response.get_raw_response_content_type()
                if isinstance(response, ConvertDocumentResponse)
                else "application/octet-stream"
            )
            return Response(
                content=raw_bytes,
                media_type=content_type or "application/octet-stream",
            )

        return response
    except Exception as e:
        processor = ProxyBaseLLMRequestProcessing(data=data)
        raise await processor._handle_llm_api_exception(
            e=e,
            user_api_key_dict=user_api_key_dict,
            proxy_logging_obj=proxy_logging_obj,
            version=version,
        )