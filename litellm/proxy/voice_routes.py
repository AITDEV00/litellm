"""OICM custom voice-management routes.

Self-contained vertical slice (VSA). Co-located next to ``proxy_server.py`` but
isolated so that upstream merges never overwrite it. Follows the same pattern as
``litellm.proxy.openrouter_compat.routes``: a standalone ``APIRouter`` that lazily
imports ``litellm.proxy.proxy_server`` module globals inside each handler to
avoid circular-import cycles (``proxy_server`` eagerly includes this router).
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any, AsyncGenerator, Dict

import orjson
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from litellm._logging import verbose_proxy_logger
from litellm.constants import AUDIO_SPEECH_CHUNK_SIZE
from litellm.proxy._types import ProxyException
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing
from litellm.proxy.common_utils.http_parsing_utils import get_form_data
from litellm.proxy.litellm_pre_call_utils import add_litellm_data_to_request
from litellm.proxy.route_llm_request import route_request

router = APIRouter(tags=["audio"])

_VOICE_DATA_KEYS = frozenset(
    {
        "speaker",
        "speaker_id",
        "voice_id",
        "name",
        "audio_url",
        "audio_path",
        "stored_path",
        "prompt_text",
        "transcript",
        "dialect",
        "global_token_ids",
        "semantic_token_ids",
        "action",
        "ref_audio",
        "ref_text",
        "profile_id",
        "overwrite",
    }
)


def _resolve_audio_model(router_instance: Any, provider: str | None = None) -> str | None:
    if provider is not None:
        prefix = f"{provider}/"
        suffix = f"/{provider}"
        for deployment in router_instance.model_list:
            litellm_params = deployment.get("litellm_params", {})
            model_str = litellm_params.get("model", "")
            if prefix in model_str or suffix in model_str:
                return deployment.get("model_name")
    for deployment in router_instance.model_list:
        litellm_params = deployment.get("litellm_params", {})
        if litellm_params.get("mode") == "audio_speech":
            return deployment.get("model_name")
    return None


def _resolve_audio_api_base(router_instance: Any, provider: str | None = None) -> str | None:
    if provider is not None:
        prefix = f"{provider}/"
        suffix = f"/{provider}"
        for deployment in router_instance.model_list:
            litellm_params = deployment.get("litellm_params", {})
            model_str = litellm_params.get("model", "")
            if prefix in model_str or suffix in model_str:
                api_base = litellm_params.get("api_base")
                if api_base:
                    return str(api_base).rstrip("/")
    for deployment in router_instance.model_list:
        litellm_params = deployment.get("litellm_params", {})
        if litellm_params.get("mode") == "audio_speech":
            api_base = litellm_params.get("api_base")
            if api_base:
                return str(api_base).rstrip("/")
    return None


def _audio_media_type(data: dict) -> str:
    media_type = "audio/mpeg"
    request_model = data.get("model", "")
    if request_model:
        request_model_lower = request_model.lower()
        if "gemini" in request_model_lower and (
            "tts" in request_model_lower or "preview-tts" in request_model_lower
        ):
            media_type = "audio/wav"
    return media_type


async def _audio_speech_chunk_generator(
    _response: Any,
) -> AsyncGenerator[bytes, None]:
    _generator = _response.aiter_bytes(chunk_size=AUDIO_SPEECH_CHUNK_SIZE)
    async for chunk in _generator:
        yield chunk


@router.post("/v1/audio/speech/clone")
@router.post("/audio/speech/clone")
async def audio_speech_clone(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    One-shot voice cloning: synthesize speech from text using a reference audio clip.

    Multipart form-data endpoint. Required: text, ref_audio (file).
    Optional: ref_text, response_format, speed, language, num_step, guidance_scale, etc.
    """
    import litellm.proxy.proxy_server as proxy_server_mod  # noqa: PLC0415  # proxy_server includes this router, so defer

    data: dict = {}
    try:
        form_data = await get_form_data(request)
        data = {key: value for key, value in form_data.items() if key != "ref_audio"}

        data = await add_litellm_data_to_request(
            data=data,
            request=request,
            general_settings=proxy_server_mod.general_settings,
            user_api_key_dict=user_api_key_dict,
            version=proxy_server_mod.version,
            proxy_config=proxy_server_mod.proxy_config,
        )

        if data.get("user", None) is None and user_api_key_dict.user_id is not None:
            data["user"] = user_api_key_dict.user_id

        if proxy_server_mod.user_model:
            data["model"] = proxy_server_mod.user_model

        if data.get("model") is None and proxy_server_mod.llm_router is not None:
            resolved = _resolve_audio_model(proxy_server_mod.llm_router)
            if resolved is not None:
                data["model"] = resolved

        ref_audio_upload = form_data.get("ref_audio")
        if ref_audio_upload is None or not hasattr(ref_audio_upload, "read"):
            raise HTTPException(status_code=400, detail="ref_audio file is required for voice cloning")

        file_content = await ref_audio_upload.read()
        data["ref_audio"] = (
            ref_audio_upload.filename or "ref_audio.wav",
            file_content,
            ref_audio_upload.content_type or "audio/wav",
        )

        text_input = data.pop("text", None)
        if text_input is None:
            raise HTTPException(status_code=400, detail="text is required for voice cloning")
        data["input"] = text_input
        data.setdefault("voice", "clone")

        data = await proxy_server_mod.proxy_logging_obj.pre_call_hook(
            user_api_key_dict=user_api_key_dict, data=data, call_type="aspeech"
        )

        llm_call = await route_request(
            data=data,
            route_type="aspeech",
            llm_router=proxy_server_mod.llm_router,
            user_model=proxy_server_mod.user_model,
        )
        response = await llm_call

        asyncio.create_task(
            proxy_server_mod.proxy_logging_obj.update_request_status(
                litellm_call_id=data.get("litellm_call_id", ""), status="success"
            )
        )

        hidden_params = getattr(response, "_hidden_params", {}) or {}
        custom_headers = ProxyBaseLLMRequestProcessing.get_custom_headers(
            user_api_key_dict=user_api_key_dict,
            model_id=hidden_params.get("model_id", None) or "",
            cache_key=hidden_params.get("cache_key", None) or "",
            api_base=hidden_params.get("api_base", None) or "",
            version=proxy_server_mod.version,
            response_cost=hidden_params.get("response_cost", None) or "",
            model_region=getattr(user_api_key_dict, "allowed_model_region", ""),
            fastest_response_batch_completion=None,
            call_id=hidden_params.get("litellm_call_id", None) or "",
            request_data=data,
            hidden_params=hidden_params,
        )

        callback_headers = await proxy_server_mod.proxy_logging_obj.post_call_response_headers_hook(
            data=data,
            user_api_key_dict=user_api_key_dict,
            response=response,
            request_headers=dict(request.headers),
        )
        if callback_headers:
            custom_headers.update(callback_headers)

        return StreamingResponse(
            _audio_speech_chunk_generator(response),
            media_type="audio/mpeg",
            headers=custom_headers,
        )

    except Exception as e:
        await proxy_server_mod.proxy_logging_obj.post_call_failure_hook(
            user_api_key_dict=user_api_key_dict, original_exception=e, request_data=data
        )
        verbose_proxy_logger.error(
            "litellm.proxy.voice_routes.audio_speech_clone(): Exception occured - {}".format(str(e))
        )
        verbose_proxy_logger.debug(traceback.format_exc())
        raise e


@router.post("/v1/audio/voices")
@router.post("/audio/voices")
async def create_voice(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    import litellm.proxy.proxy_server as proxy_server_mod  # noqa: PLC0415  # proxy_server includes this router, so defer

    data: Dict = {}
    try:
        body = await request.body()
        data = orjson.loads(body)

        data = await add_litellm_data_to_request(
            data=data,
            request=request,
            general_settings=proxy_server_mod.general_settings,
            user_api_key_dict=user_api_key_dict,
            version=proxy_server_mod.version,
            proxy_config=proxy_server_mod.proxy_config,
        )

        if data.get("user", None) is None and user_api_key_dict.user_id is not None:
            data["user"] = user_api_key_dict.user_id

        if proxy_server_mod.user_model:
            data["model"] = proxy_server_mod.user_model

        model = data.pop("model", None) or proxy_server_mod.user_model
        voice_data = {k: data.pop(k) for k in list(data.keys()) if k in _VOICE_DATA_KEYS}
        data = {"model": model, "voice_data": voice_data, **data}

        data = await proxy_server_mod.proxy_logging_obj.pre_call_hook(
            user_api_key_dict=user_api_key_dict, data=data, call_type="acreate_voice"
        )

        llm_call = await route_request(
            data=data,
            route_type="acreate_voice",
            llm_router=proxy_server_mod.llm_router,
            user_model=proxy_server_mod.user_model,
        )
        response = await llm_call

        asyncio.create_task(
            proxy_server_mod.proxy_logging_obj.update_request_status(
                litellm_call_id=data.get("litellm_call_id", ""), status="success"
            )
        )

        hidden_params = getattr(response, "_hidden_params", {}) or {}
        model_id = hidden_params.get("model_id", None) or ""
        cache_key = hidden_params.get("cache_key", None) or ""
        api_base = hidden_params.get("api_base", None) or ""
        response_cost = hidden_params.get("response_cost", None) or ""
        litellm_call_id = hidden_params.get("litellm_call_id", None) or ""

        custom_headers = ProxyBaseLLMRequestProcessing.get_custom_headers(
            user_api_key_dict=user_api_key_dict,
            model_id=model_id,
            cache_key=cache_key,
            api_base=api_base,
            version=proxy_server_mod.version,
            response_cost=response_cost,
            model_region=getattr(user_api_key_dict, "allowed_model_region", ""),
            fastest_response_batch_completion=None,
            call_id=litellm_call_id,
            request_data=data,
            hidden_params=hidden_params,
        )

        return JSONResponse(content=response, headers=custom_headers)

    except Exception as e:
        await proxy_server_mod.proxy_logging_obj.post_call_failure_hook(
            user_api_key_dict=user_api_key_dict, original_exception=e, request_data=data
        )
        verbose_proxy_logger.error("litellm.proxy.voice_routes.create_voice(): Exception occured - {}".format(str(e)))
        verbose_proxy_logger.debug(traceback.format_exc())
        raise e


async def _route_voice_management(
    request: Request,
    user_api_key_dict: UserAPIKeyAuth,
    action: str,
    route_type: str,
    profile_id: str | None = None,
) -> JSONResponse:
    import litellm.proxy.proxy_server as proxy_server_mod  # noqa: PLC0415  # proxy_server includes this router, so defer

    data: Dict = {}
    try:
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            form_data = await get_form_data(request)
            raw = {key: value for key, value in form_data.items() if key != "ref_audio"}
            ref_audio_upload = form_data.get("ref_audio")
            if ref_audio_upload is not None and hasattr(ref_audio_upload, "read"):
                file_content = await ref_audio_upload.read()
                raw["ref_audio"] = (
                    ref_audio_upload.filename or "ref_audio.wav",
                    file_content,
                    ref_audio_upload.content_type or "audio/wav",
                )
        else:
            body = await request.body()
            raw = orjson.loads(body) if body else {}

        data = await add_litellm_data_to_request(
            data=raw,
            request=request,
            general_settings=proxy_server_mod.general_settings,
            user_api_key_dict=user_api_key_dict,
            version=proxy_server_mod.version,
            proxy_config=proxy_server_mod.proxy_config,
        )

        if data.get("user", None) is None and user_api_key_dict.user_id is not None:
            data["user"] = user_api_key_dict.user_id

        model = data.pop("model", None) or proxy_server_mod.user_model

        if model is None and proxy_server_mod.llm_router is not None:
            model = _resolve_audio_model(proxy_server_mod.llm_router)

        voice_data = {k: data.pop(k) for k in list(data.keys()) if k in _VOICE_DATA_KEYS}
        voice_data["action"] = action
        if profile_id is not None:
            voice_data["profile_id"] = profile_id
        data = {"model": model, "voice_data": voice_data, **data}

        data = await proxy_server_mod.proxy_logging_obj.pre_call_hook(
            user_api_key_dict=user_api_key_dict, data=data, call_type=route_type
        )

        llm_call = await route_request(
            data=data,
            route_type=route_type,
            llm_router=proxy_server_mod.llm_router,
            user_model=proxy_server_mod.user_model,
        )
        response = await llm_call

        asyncio.create_task(
            proxy_server_mod.proxy_logging_obj.update_request_status(
                litellm_call_id=data.get("litellm_call_id", ""), status="success"
            )
        )

        hidden_params = getattr(response, "_hidden_params", {}) or {}
        custom_headers = ProxyBaseLLMRequestProcessing.get_custom_headers(
            user_api_key_dict=user_api_key_dict,
            model_id=hidden_params.get("model_id", None) or "",
            cache_key=hidden_params.get("cache_key", None) or "",
            api_base=hidden_params.get("api_base", None) or "",
            version=proxy_server_mod.version,
            response_cost=hidden_params.get("response_cost", None) or "",
            model_region=getattr(user_api_key_dict, "allowed_model_region", ""),
            fastest_response_batch_completion=None,
            call_id=hidden_params.get("litellm_call_id", None) or "",
            request_data=data,
            hidden_params=hidden_params,
        )

        return JSONResponse(content=response, headers=custom_headers)

    except Exception as e:
        await proxy_server_mod.proxy_logging_obj.post_call_failure_hook(
            user_api_key_dict=user_api_key_dict, original_exception=e, request_data=data
        )
        verbose_proxy_logger.error(
            "litellm.proxy.voice_routes._route_voice_management(): Exception occured - {}".format(str(e))
        )
        verbose_proxy_logger.debug(traceback.format_exc())
        raise e


@router.get("/v1/voices")
@router.get("/voices")
async def list_voices(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    return await _route_voice_management(
        request=request, user_api_key_dict=user_api_key_dict, action="list", route_type="acreate_voice"
    )


@router.get("/v1/voices/profiles")
@router.get("/voices/profiles")
async def list_voice_profiles(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    return await _route_voice_management(
        request=request, user_api_key_dict=user_api_key_dict, action="list_profiles", route_type="acreate_voice"
    )


@router.get("/v1/voices/profiles/{profile_id}")
@router.get("/voices/profiles/{profile_id}")
async def get_voice_profile(
    request: Request,
    fastapi_response: Response,
    profile_id: str = Path(...),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    return await _route_voice_management(
        request=request,
        user_api_key_dict=user_api_key_dict,
        action="get_profile",
        route_type="acreate_voice",
        profile_id=profile_id,
    )


@router.post("/v1/voices/profiles")
@router.post("/voices/profiles")
async def create_voice_profile(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    return await _route_voice_management(
        request=request,
        user_api_key_dict=user_api_key_dict,
        action="create_profile",
        route_type="acreate_voice",
    )


@router.patch("/v1/voices/profiles/{profile_id}")
@router.patch("/voices/profiles/{profile_id}")
async def update_voice_profile(
    request: Request,
    fastapi_response: Response,
    profile_id: str = Path(...),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    return await _route_voice_management(
        request=request,
        user_api_key_dict=user_api_key_dict,
        action="update_profile",
        route_type="acreate_voice",
        profile_id=profile_id,
    )


@router.delete("/v1/voices/profiles/{profile_id}")
@router.delete("/voices/profiles/{profile_id}")
async def delete_voice_profile(
    request: Request,
    fastapi_response: Response,
    profile_id: str = Path(...),
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    return await _route_voice_management(
        request=request,
        user_api_key_dict=user_api_key_dict,
        action="delete_profile",
        route_type="acreate_voice",
        profile_id=profile_id,
    )


@router.post("/v1/audio/script")
@router.post("/audio/script")
async def audio_script(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """
    OmniVoice multi-speaker script synthesis.
    """
    import litellm.proxy.proxy_server as proxy_server_mod  # noqa: PLC0415  # proxy_server includes this router, so defer

    data: Dict = {}
    try:
        body = await request.body()
        raw = orjson.loads(body) if body else {}

        data = await add_litellm_data_to_request(
            data=raw,
            request=request,
            general_settings=proxy_server_mod.general_settings,
            user_api_key_dict=user_api_key_dict,
            version=proxy_server_mod.version,
            proxy_config=proxy_server_mod.proxy_config,
        )

        if data.get("user", None) is None and user_api_key_dict.user_id is not None:
            data["user"] = user_api_key_dict.user_id

        model = data.pop("model", None) or proxy_server_mod.user_model

        if model is None and proxy_server_mod.llm_router is not None:
            model = _resolve_audio_model(proxy_server_mod.llm_router)

        script_segments = data.pop("script", None)
        if script_segments is None:
            raise HTTPException(status_code=400, detail="script is required for script synthesis")

        data["script"] = script_segments
        data.setdefault("model", model)
        data.setdefault("input", "")
        data.setdefault("voice", None)

        data = await proxy_server_mod.proxy_logging_obj.pre_call_hook(
            user_api_key_dict=user_api_key_dict, data=data, call_type="ascript"
        )

        llm_call = await route_request(
            data=data,
            route_type="ascript",
            llm_router=proxy_server_mod.llm_router,
            user_model=proxy_server_mod.user_model,
        )
        response = await llm_call

        asyncio.create_task(
            proxy_server_mod.proxy_logging_obj.update_request_status(
                litellm_call_id=data.get("litellm_call_id", ""), status="success"
            )
        )

        hidden_params = getattr(response, "_hidden_params", {}) or {}
        custom_headers = ProxyBaseLLMRequestProcessing.get_custom_headers(
            user_api_key_dict=user_api_key_dict,
            model_id=hidden_params.get("model_id", None) or "",
            cache_key=hidden_params.get("cache_key", None) or "",
            api_base=hidden_params.get("api_base", None) or "",
            version=proxy_server_mod.version,
            response_cost=hidden_params.get("response_cost", None) or "",
            model_region=getattr(user_api_key_dict, "allowed_model_region", ""),
            fastest_response_batch_completion=None,
            call_id=hidden_params.get("litellm_call_id", None) or "",
            request_data=data,
            hidden_params=hidden_params,
        )

        if isinstance(response, dict):
            return JSONResponse(content=response, headers=custom_headers)

        return StreamingResponse(
            _audio_speech_chunk_generator(response),
            media_type="audio/mpeg",
            headers=custom_headers,
        )

    except Exception as e:
        await proxy_server_mod.proxy_logging_obj.post_call_failure_hook(
            user_api_key_dict=user_api_key_dict, original_exception=e, request_data=data
        )
        verbose_proxy_logger.error("litellm.proxy.voice_routes.audio_script(): Exception - {}".format(str(e)))
        verbose_proxy_logger.debug(traceback.format_exc())
        raise e


@router.get("/v1/audio/models")
@router.get("/audio/models")
async def audio_models(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> Response:
    return await _proxy_to_audio_pod(request, user_api_key_dict, "/v1/models")


@router.get("/v1/audio/models/{model_id}")
@router.get("/audio/models/{model_id}")
async def audio_model_detail(
    model_id: str,
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> Response:
    return await _proxy_to_audio_pod(request, user_api_key_dict, f"/v1/models/{model_id}")


@router.get("/v1/audio/health")
@router.get("/audio/health")
async def audio_health(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> Response:
    return await _proxy_to_audio_pod(request, user_api_key_dict, "/health")


@router.get("/v1/audio/metrics")
@router.get("/audio/metrics")
async def audio_metrics(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
) -> Response:
    return await _proxy_to_audio_pod(request, user_api_key_dict, "/metrics")


async def _proxy_to_audio_pod(
    request: Request,
    user_api_key_dict: UserAPIKeyAuth,
    path: str,
    provider: str = "omnivoice",
) -> Response:
    import httpx

    import litellm.proxy.proxy_server as proxy_server_mod  # noqa: PLC0415  # proxy_server includes this router, so defer

    llm_router = proxy_server_mod.llm_router
    if llm_router is None:
        raise ProxyException(
            message="No router configured",
            code=503,
            type="server_error",
            param=None,
        )

    api_base = _resolve_audio_api_base(llm_router, provider=provider)
    if api_base is None:
        raise ProxyException(
            message="No {} deployment found".format(provider),
            code=503,
            type="server_error",
            param=None,
        )

    target_url = api_base + path

    ssl_verify = True
    timeout = 30.0
    for deployment in llm_router.model_list:
        litellm_params = deployment.get("litellm_params", {})
        model_str = litellm_params.get("model", "")
        if f"{provider}/" in model_str or f"/{provider}" in model_str:
            ssl_verify = litellm_params.get("ssl_verify", True)
            request_timeout = litellm_params.get("request_timeout")
            if request_timeout is not None:
                timeout = float(request_timeout)
            break

    async with httpx.AsyncClient(verify=ssl_verify, timeout=timeout) as client:
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length", "authorization")
        }
        resp = await client.get(target_url, headers=headers, params=dict(request.query_params))

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers={k: v for k, v in resp.headers.items() if k.lower() not in ("content-encoding", "transfer-encoding")},
    )