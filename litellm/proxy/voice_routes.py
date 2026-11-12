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
from collections.abc import Iterator
from typing import Any, AsyncGenerator, Dict, Final

import orjson
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from litellm._logging import verbose_proxy_logger
from litellm.constants import AUDIO_SPEECH_CHUNK_SIZE
from litellm.litellm_core_utils.audio_utils.utils import resolve_speech_media_type
from litellm.proxy._types import ProxyException
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing
from litellm.proxy.common_utils.http_parsing_utils import get_form_data
from litellm.proxy.litellm_pre_call_utils import add_litellm_data_to_request
from litellm.proxy.route_llm_request import route_request
from litellm.types.llms.openai import HttpxBinaryResponseContent

router = APIRouter(tags=["audio"])

# Routes on this router that are pod diagnostics rather than inference, so they
# stay off the llm_api_routes allowlist. Every other path here must be reachable
# by a key_type=llm_api key; tests/unit/proxy/test_oicm_drop_detection.py fails
# on any route that is in neither set, so a new route has to be classified
# explicitly instead of silently defaulting to denied.
OPS_ONLY_ROUTES: Final[frozenset[str]] = frozenset(
    {
        "/audio/health",
        "/v1/audio/health",
        "/audio/metrics",
        "/v1/audio/metrics",
    }
)

# Read-only model discovery, proxied straight to the pod's /v1/models. Kept in
# the llm_api_routes allowlist (a key that can call a model can list it) but, like
# upstream's own /v1/models, it has no CallTypes entry because it invokes no model
# and therefore has nothing for a guardrail to scan.
DISCOVERY_ROUTES: Final[frozenset[str]] = frozenset(
    {
        "/audio/models",
        "/v1/audio/models",
        "/audio/models/{model_id}",
        "/v1/audio/models/{model_id}",
    }
)

_API_VERSION_PREFIX = "/v1"

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
        # The sglang-omni voice upload endpoint requires a consent string
        # alongside the sample; without this key the register action can never
        # carry it through voice_data and the pod rejects the upload.
        "consent",
    }
)


def _deployment_mode(deployment: dict) -> str | None:
    """The deployment's mode, which LiteLLM stores in either of two places.

    Deployments created from a config file carry ``mode`` in ``litellm_params``,
    while ones created through the model-management API carry it in
    ``model_info``. Reading only one of the two silently skipped every
    deployment registered the other way, so an audio route could resolve to a
    deployment whose pod no longer exists.
    """
    litellm_params: Final = deployment.get("litellm_params") or {}
    mode: Final = litellm_params.get("mode")
    if mode is not None:
        return str(mode)
    model_info: Final = deployment.get("model_info") or {}
    return model_info.get("mode")


def _audio_deployments_by_priority(router_instance: Any, provider: str | None) -> Iterator[dict]:
    """Audio deployments to consider, best first.

    A provider-qualified match outranks any deployment that merely declares the
    audio_speech mode, so an explicitly requested provider is never shadowed by
    whichever deployment happens to be listed first. When a provider is named
    but has no deployment of its own, nothing is yielded: falling through to an
    unrelated provider's pod would forward the request to a server that cannot
    serve it.
    """
    if provider is not None:
        prefix = f"{provider}/"
        suffix = f"/{provider}"
        for deployment in router_instance.model_list:
            model_str = deployment.get("litellm_params", {}).get("model", "")
            if prefix in model_str or suffix in model_str:
                yield deployment
        return
    for deployment in router_instance.model_list:
        if _deployment_mode(deployment) == "audio_speech":
            yield deployment


def _resolve_audio_model(router_instance: Any, provider: str | None = None) -> str | None:
    for deployment in _audio_deployments_by_priority(router_instance, provider):
        return deployment.get("model_name")
    return None


def _voice_capable_providers(action: str) -> frozenset[str]:
    """Providers whose deployments can serve this voice-management action.

    Derived from the provider registry and each provider's declared action
    set, so adding voice support for a new provider makes it eligible here
    automatically.
    """
    import litellm  # noqa: PLC0415  # deferred like every other import in this router
    from litellm.llms.oicm_providers import get_provider_voice_config

    return frozenset(
        member.value
        for member in litellm.LlmProviders
        if (config := get_provider_voice_config(member)) is not None and action in config.SUPPORTED_VOICE_ACTIONS
    )


def _resolve_voice_model(router_instance: Any, action: str) -> str | None:
    """A deployment that can actually serve this voice-management action.

    The plain audio fallback returns whichever audio_speech deployment is
    registered first, which on this gateway is a provider with no voice
    management at all: every no-model voice request then failed with a 400
    from that provider instead of reaching one that supports the action.
    Providers that support voice management but not this specific action
    (e.g. a backend with no list API) are skipped for the same reason.
    """
    capable: Final = _voice_capable_providers(action)
    for deployment in _audio_deployments_by_priority(router_instance, provider=None):
        model_str = str(deployment.get("litellm_params", {}).get("model", ""))
        if model_str.split("/", 1)[0] in capable:
            return deployment.get("model_name")
    return None


# Upstream LiteLLM surfaces routing/fallback decisions as x-litellm-* headers
# (see router_utils/add_retry_fallback_headers.py for the retry/fallback set).
# Follow the same convention: a caller that does not pass ?model= should be
# able to tell which model actually served the response without reading the
# response body's pod-native shape.
VOICE_RESOLUTION_HEADER: Final = "x-litellm-voice-resolution"
VOICE_RESOLVED_MODEL_HEADER: Final = "x-litellm-voice-resolved-model"


def _voice_resolution_headers(resolved_model: str | None, fallback: bool) -> dict[str, str]:
    """Headers telling the caller whether we served their explicit model or fell back.

    `explicit` means the caller passed ?model= or a body model field. `fallback`
    means we picked the first voice-capable deployment for the action, which is
    almost never what an end-user request intends, but is kept for backward
    compatibility with pre-scoping clients. Explicit callers see the resolved
    model name echoed; fallback callers see it plus the `fallback` marker, so
    an absent ?model= never goes silently to a different pod than expected.
    """
    if resolved_model is None:
        return {}
    return {
        VOICE_RESOLUTION_HEADER: "fallback" if fallback else "explicit",
        VOICE_RESOLVED_MODEL_HEADER: resolved_model,
    }


def _resolve_audio_api_base(router_instance: Any, provider: str | None = None) -> str | None:
    for deployment in _audio_deployments_by_priority(router_instance, provider):
        api_base = deployment.get("litellm_params", {}).get("api_base")
        if api_base:
            return str(api_base).rstrip("/")
    return None


def _join_audio_url(api_base: str, path: str) -> str:
    """Anchor a pod path at the deployment's host root.

    Audio deployments register their api_base as the OpenAI-compatible base,
    e.g. ``http://host:8080/v1``, but the routes here forward absolute pod paths:
    ``/v1/models`` and ``/v1/voices`` live under the version prefix while
    ``/health`` and ``/metrics`` live at the root. Concatenating the base as-is
    therefore requested either ``/v1/v1/models`` or ``/v1/health``, and the pod
    answered 404 for both.
    """
    base: Final = api_base.rstrip("/")
    root: Final = base[: -len(_API_VERSION_PREFIX)] if base.endswith(_API_VERSION_PREFIX) else base
    suffix: Final = path if path.startswith("/") else f"/{path}"
    return root + suffix


def _resolve_audio_target_url(llm_router: Any, provider: str, path: str) -> str | None:
    """The pod URL the custom audio routes forward to, or None when unregistered."""
    api_base: Final = _resolve_audio_api_base(llm_router, provider=provider)
    if api_base is None:
        return None
    return _join_audio_url(api_base, path)


def _audio_media_type(data: dict, response: HttpxBinaryResponseContent | object) -> str:
    raw_content_type: Final = response.content_type if isinstance(response, HttpxBinaryResponseContent) else None
    content_type: Final = raw_content_type if isinstance(raw_content_type, str) else None
    hidden_params: Final = getattr(response, "hidden_params", {}) or {}
    actual_format: Final = hidden_params.get("response_format")
    requested_format: Final = actual_format if isinstance(actual_format, str) else data.get("response_format")
    return resolve_speech_media_type(
        upstream_content_type=None if actual_format is not None else content_type,
        response_format=requested_format if isinstance(requested_format, str) else None,
    )


async def _audio_speech_chunk_generator(
    _response: Any,
) -> AsyncGenerator[bytes, None]:
    _generator = await _response.aiter_bytes(chunk_size=AUDIO_SPEECH_CHUNK_SIZE)
    try:
        async for chunk in _generator:
            yield chunk
    finally:
        await _response.aclose()


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
            media_type=_audio_media_type(data, response),
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
        # The sglang-omni voice upload is a multipart request (audio file plus
        # name/consent form fields). Parsing the raw body as JSON 500s on any
        # multipart call, so branch on the content type the same way
        # _route_voice_management below does; voice upload is the one action
        # whose callers genuinely send files.
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" in content_type:
            try:
                form_data = await get_form_data(request)
            except RuntimeError as e:
                if str(e) != "Stream consumed":
                    raise
                form_data = await request.form()
            raw = {key: value for key, value in form_data.items() if key != "ref_audio"}
            ref_audio_upload = form_data.get("ref_audio")
            if ref_audio_upload is not None and hasattr(ref_audio_upload, "read"):
                file_content = await ref_audio_upload.read()
                raw["ref_audio"] = (
                    ref_audio_upload.filename or "ref_audio.wav",
                    file_content,
                    ref_audio_upload.content_type or "audio/wav",
                )
            data = raw
        else:
            body = await request.body()
            data = orjson.loads(body) if body else {}

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

        # Same query-param scoping as _route_voice_management: the route resolves
        # the deployment from a body/form field, but ?model= is the natural way
        # to name it and was being ignored.
        explicit_model: Final = (
            request.query_params.get("model") or data.pop("model", None) or proxy_server_mod.user_model
        )
        used_fallback: Final = explicit_model is None

        resolved_model: Final = (
            explicit_model
            if explicit_model is not None
            else (
                _resolve_voice_model(proxy_server_mod.llm_router, action="register")
                if proxy_server_mod.llm_router is not None
                else None
            )
        )

        voice_data = {k: data.pop(k) for k in list(data.keys()) if k in _VOICE_DATA_KEYS}
        # The route owns the action. Without this a client could POST here with
        # action="delete_profile" and drive a different pod operation through
        # the create endpoint.
        voice_data["action"] = "register"
        data = {"model": resolved_model, "voice_data": voice_data, **data}

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

        if isinstance(response, dict):
            response.pop("_hidden_params", None)

        return JSONResponse(
            content=response,
            headers={**custom_headers, **_voice_resolution_headers(resolved_model, used_fallback)},
        )

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

        # GET routes carry no body, so a caller scoping with ?model= would
        # otherwise silently fall through to _resolve_voice_model, which returns
        # the first voice-capable deployment regardless of what was requested.
        explicit_model: Final = (
            request.query_params.get("model") or data.pop("model", None) or proxy_server_mod.user_model
        )
        used_fallback: Final = explicit_model is None

        resolved_model: Final = (
            explicit_model
            if explicit_model is not None
            else (
                _resolve_voice_model(proxy_server_mod.llm_router, action=action)
                if proxy_server_mod.llm_router is not None
                else None
            )
        )

        voice_data = {k: data.pop(k) for k in list(data.keys()) if k in _VOICE_DATA_KEYS}
        voice_data["action"] = action
        if profile_id is not None:
            voice_data["profile_id"] = profile_id
        data = {"model": resolved_model, "voice_data": voice_data, **data}

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

        if isinstance(response, dict):
            response.pop("_hidden_params", None)

        return JSONResponse(
            content=response,
            headers={**custom_headers, **_voice_resolution_headers(resolved_model, used_fallback)},
        )

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
            response.pop("_hidden_params", None)
            return JSONResponse(content=response, headers=custom_headers)

        return StreamingResponse(
            _audio_speech_chunk_generator(response),
            media_type=_audio_media_type(data, response),
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

    # The provider is a routing hint from the caller. A hardcoded "omnivoice"
    # default would always win the truthy slot in an `or` chain, so it cannot be
    # used as the fallback. The caller's explicit query param takes precedence,
    # and only when both are absent do we drop to the single-default behavior.
    explicit_provider: Final = request.query_params.get("provider")
    used_fallback: Final = explicit_provider is None
    provider_hint: Final = explicit_provider or provider or "omnivoice"

    target_url: Final = _resolve_audio_target_url(llm_router, provider_hint, path)
    if target_url is None:
        raise ProxyException(
            message="No {} deployment found".format(provider_hint),
            code=503,
            type="server_error",
            param=None,
        )

    ssl_verify = True
    timeout = 30.0
    for deployment in llm_router.model_list:
        litellm_params = deployment.get("litellm_params", {})
        model_str = litellm_params.get("model", "")
        if f"{provider_hint}/" in model_str or f"/{provider_hint}" in model_str:
            ssl_verify = litellm_params.get("ssl_verify", True)
            request_timeout = litellm_params.get("request_timeout")
            if request_timeout is not None:
                timeout = float(request_timeout)
            break

    async with httpx.AsyncClient(verify=ssl_verify, timeout=timeout) as client:
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length", "authorization")
        }
        # `provider` is consumed for resolution above; it is not a pod parameter,
        # so forwarding it would name a query key the pod does not understand.
        forward_params: Final = {k: v for k, v in request.query_params.items() if k != "provider"}
        resp = await client.get(target_url, headers=headers, params=forward_params)

    # Surface the provider actually hit. `x-litellm-voice-resolution` names
    # whether the caller asked for this provider or we defaulted; the resolved
    # header names the provider itself (not a model id, because these routes
    # proxy the pod's generic models/health/metrics endpoints).
    resolution_headers: Final = {
        VOICE_RESOLUTION_HEADER: "fallback" if used_fallback else "explicit",
        VOICE_RESOLVED_MODEL_HEADER: provider_hint,
    }
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
        headers={
            **{k: v for k, v in resp.headers.items() if k.lower() not in ("content-encoding", "transfer-encoding")},
            **resolution_headers,
        },
    )
