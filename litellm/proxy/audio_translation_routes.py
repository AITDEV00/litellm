"""OICM custom audio translation routes: POST /v1/audio/translations.

Self-contained vertical slice (VSA) following the ``diarization_routes.py``
pattern: a standalone ``APIRouter`` that lazily imports
``litellm.proxy.proxy_server`` module globals inside the handler to avoid
circular-import cycles (``proxy_server`` eagerly includes this router).

The contract is the OpenAI ``POST /v1/audio/translations`` multipart surface
served by native audio deployments (hosted-vLLM, NVIDIA NIM) and the official
OpenAI SDK transport for ``openai`` deployments: multipart ``file`` plus form
fields, provider extension fields (e.g. NIM's ``target_language``) forwarded
verbatim through native multipart, and the response returned as native JSON or
as a true document (text/SRT/VTT) under the provider's declared media type.
``stream=true`` on a deployment that does not support multipart streaming must
surface the upstream 400 truthfully, never a rewritten error.
"""

from __future__ import annotations

import asyncio
import io
from typing import Final

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status

from litellm.proxy._types import ProxyException, UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.common_request_processing import log_llm_api_exception, resolve_litellm_call_id
from litellm.proxy.common_utils.http_parsing_utils import (
    check_file_size_under_limit,
    get_form_data,
    resolve_inference_model,
)
from litellm.proxy.common_utils.openai_error_payload import litellm_call_id_headers
from litellm.proxy.litellm_pre_call_utils import add_litellm_data_to_request
from litellm.proxy.route_llm_request import route_request

router = APIRouter(tags=["audio"])

# Translation is inference; no pod-diagnostics paths are served here.
OPS_ONLY_ROUTES: Final[frozenset[str]] = frozenset()
DISCOVERY_ROUTES: Final[frozenset[str]] = frozenset()


@router.post(
    "/v1/audio/translations",
    dependencies=[Depends(user_api_key_auth)],
    tags=["audio"],
)
@router.post(
    "/audio/translations",
    dependencies=[Depends(user_api_key_auth)],
    tags=["audio"],
)
async def audio_translations(
    request: Request,
    fastapi_response: Response,
    file: UploadFile = File(...),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    Same params as:

    https://platform.openai.com/docs/api-reference/audio/createTranslation?lang=curl
    """
    from litellm.proxy import proxy_server  # noqa: PLC0415  # proxy_server includes this router, so defer

    litellm_call_id: Final = resolve_litellm_call_id(request.headers.get("x-litellm-call-id"))
    data: dict = {"litellm_call_id": litellm_call_id}
    try:
        form_data: Final = await get_form_data(request)
        data = {**{key: value for key, value in form_data.items() if key != "file"}, **data}

        data = await add_litellm_data_to_request(
            data=data,
            request=request,
            general_settings=proxy_server.general_settings,
            user_api_key_dict=user_api_key_dict,
            version=proxy_server.version,
            proxy_config=proxy_server.proxy_config,
        )

        if data.get("user", None) is None and user_api_key_dict.user_id is not None:
            data["user"] = user_api_key_dict.user_id

        # kind="body": the client-facing model alias is the deployment group
        # name, deliberately not the moderation setting fallback.
        data["model"] = resolve_inference_model(
            data.get("model"), proxy_server.general_settings, proxy_server.user_model, kind="body"
        )

        router_model_names: Final = proxy_server.llm_router.model_names if proxy_server.llm_router is not None else []

        if file.filename is None:
            raise ProxyException(
                message="File name is None. Please check your file name",
                code=status.HTTP_400_BAD_REQUEST,
                type="bad_request",
                param="file",
            )

        check_file_size_under_limit(
            request_data=data,
            file=file,
            router_model_names=router_model_names,
        )

        file_content: Final = await file.read()
        file_object = io.BytesIO(file_content)
        file_object.name = file.filename
        data["file"] = file_object

        try:
            ### CALL HOOKS ### - modify incoming data / reject request before calling the model
            data = await proxy_server.proxy_logging_obj.pre_call_hook(
                user_api_key_dict=user_api_key_dict,
                data=data,
                call_type="atranslation",
            )

            ## ROUTE TO CORRECT ENDPOINT ##
            llm_call: Final = await route_request(
                data=data,
                route_type="atranslation",
                llm_router=proxy_server.llm_router,
                user_model=proxy_server.user_model,
            )
            response: Final = await llm_call
        finally:
            file_object.close()  # close the file read in by io library

        ### ALERTING ###
        asyncio.create_task(
            proxy_server.proxy_logging_obj.update_request_status(litellm_call_id=litellm_call_id, status="success")
        )

        from litellm.proxy.audio_response_helpers import (  # noqa: PLC0415  # co-located audio slice, defer to import time
            apply_audio_post_call_processing,
            apply_audio_response_headers_hook,
            audio_document_response,
            audio_json_response,
            build_audio_custom_headers,
        )
        from litellm.types.llms.audio_transcription import (  # noqa: PLC0415  # type-only slice import, defer
            NativeAudioDocumentResponse,
            TranscriptionResponse,
        )

        if isinstance(response, (TranscriptionResponse, NativeAudioDocumentResponse)):
            response = await apply_audio_post_call_processing(
                proxy_logging_obj=proxy_server.proxy_logging_obj,
                data=data,
                user_api_key_dict=user_api_key_dict,
                response=response,
            )

        ### RESPONSE HEADERS ###
        fastapi_response.headers.update(
            build_audio_custom_headers(
                response=response,
                data=data,
                user_api_key_dict=user_api_key_dict,
                version=proxy_server.version,
                request_data=data,
            )
        )

        callback_headers: Final = await apply_audio_response_headers_hook(
            proxy_logging_obj=proxy_server.proxy_logging_obj,
            data=data,
            user_api_key_dict=user_api_key_dict,
            response=response,
            request_headers=dict(request.headers),
        )
        # The handler returns an explicit Response, so FastAPI does not merge
        # the injected response parameter's headers: they must ride the returned
        # Response itself
        response_headers: Final[dict[str, str]] = {
            **dict(fastapi_response.headers),
            **callback_headers,
        }
        if isinstance(response, NativeAudioDocumentResponse):
            return audio_document_response(response, headers=response_headers)
        return audio_json_response(response, headers=response_headers)
    except Exception as e:
        await proxy_server.proxy_logging_obj.post_call_failure_hook(
            user_api_key_dict=user_api_key_dict, original_exception=e, request_data=data
        )
        log_llm_api_exception(e, litellm_call_id)
        if isinstance(e, HTTPException):
            raise ProxyException(
                message=getattr(e, "message", str(e.detail)),
                type=getattr(e, "type", "None"),
                param=getattr(e, "param", "None"),
                headers=litellm_call_id_headers(litellm_call_id),
                code=getattr(e, "status_code", status.HTTP_400_BAD_REQUEST),
            )
        error_msg: Final = f"{e}"
        raise ProxyException(
            message=getattr(e, "message", error_msg),
            type=getattr(e, "type", "None"),
            param=getattr(e, "param", "None"),
            headers=litellm_call_id_headers(litellm_call_id),
            openai_code=getattr(e, "code", None),
            code=getattr(e, "status_code", 500),
        )
