"""OICM custom diarization route: POST /v1/diarize.

Self-contained vertical slice (VSA). Co-located next to ``proxy_server.py`` but
isolated so that upstream merges never overwrite it, following the
``voice_routes.py`` pattern: a standalone ``APIRouter`` that lazily imports
``litellm.proxy.proxy_server`` module globals inside the handler to avoid
circular-import cycles (``proxy_server`` eagerly includes this router).

The contract is the pyannoteAI cloud ``POST /v1/diarize`` served by coro
deployments: multipart ``file`` + form fields forwarded verbatim, the pod's
``DiarizationJob`` JSON returned as-is, and the pod's per-field validation
400s passed through untouched. The serving model id on ADEO deployments is
``pyannote/community-1``; the pod's hardcoded ``community-1`` form default is
rejected there, so this route always stamps ``model`` before dispatch.
"""

from __future__ import annotations

import asyncio
import traceback
from typing import Any, Dict, Final

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from litellm._logging import verbose_proxy_logger
from litellm.proxy.auth.user_api_key_auth import UserAPIKeyAuth, user_api_key_auth
from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing
from litellm.proxy.common_utils.http_parsing_utils import get_form_data
from litellm.proxy.litellm_pre_call_utils import add_litellm_data_to_request
from litellm.proxy.route_llm_request import route_request

router = APIRouter(tags=["audio"])

# Pod diagnostics rather than inference; see the note in voice_routes.py. This
# router serves only the diarize contract today, which is neither.
OPS_ONLY_ROUTES: Final[frozenset[str]] = frozenset()
DISCOVERY_ROUTES: Final[frozenset[str]] = frozenset()

_KNOWN_LITELLM_PARAMS = frozenset(
    {
        "model",
        "exclusive",
        "numSpeakers",
        "minSpeakers",
        "maxSpeakers",
        "confidence",
        "turnLevelConfidence",
        "transcription",
        "transcriptionConfig",
        "vadSensitivity",
        "crosstalkSensitivity",
        "speechProbability",
        "crosstalkProbability",
        "speakerProbability",
        "webhook",
        "webhookStatusOnly",
    }
)


def _deployment_mode(deployment: dict) -> str | None:
    """The deployment's mode, which LiteLLM stores in either of two places.

    Deployments created from a config file carry ``mode`` in ``litellm_params``,
    while ones created through the model-management API carry it in
    ``model_info``. Reading only one of the two silently skips every deployment
    registered the other way.
    """
    mode = (deployment.get("litellm_params") or {}).get("mode")
    if mode is not None:
        return str(mode)
    return (deployment.get("model_info") or {}).get("mode")


def _resolve_diarization_model(router_instance: Any) -> str | None:
    for deployment in router_instance.model_list:
        model_str = str(deployment.get("litellm_params", {}).get("model", ""))
        if model_str.startswith("pyannote/"):
            return deployment.get("model_name")
    for deployment in router_instance.model_list:
        if _deployment_mode(deployment) == "diarization":
            return deployment.get("model_name")
    return None


@router.post("/v1/diarize")
@router.post("/diarize")
async def diarize(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    """Diarize audio following the pyannoteAI ``POST /v1/diarize`` conventions."""
    import litellm.proxy.proxy_server as proxy_server_mod  # noqa: PLC0415  # proxy_server includes this router, so defer

    data: Dict = {}
    try:
        form_data = await get_form_data(request)
        data = {
            key: value
            for key, value in form_data.items()
            if key != "file" and key in _KNOWN_LITELLM_PARAMS
        }

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
            resolved = _resolve_diarization_model(proxy_server_mod.llm_router)
            if resolved is not None:
                data["model"] = resolved

        if data.get("model") is None:
            raise HTTPException(
                status_code=400,
                detail="model is required: pass the model form field or register a diarization deployment",
            )

        model: str = data.pop("model")

        file_upload = form_data.get("file")
        if file_upload is None or not hasattr(file_upload, "read"):
            raise HTTPException(status_code=400, detail="file form field is required")
        file_content = await file_upload.read()
        audio_file = (
            file_upload.filename or "audio.wav",
            file_content,
            file_upload.content_type or "audio/wav",
        )

        form_fields = {
            key: value
            for key, value in form_data.items()
            if key not in ("file", "model")
        }

        data = await proxy_server_mod.proxy_logging_obj.pre_call_hook(
            user_api_key_dict=user_api_key_dict, data=data, call_type="adiarize"
        )

        data["model"] = model
        data["form_fields"] = form_fields
        data["audio_file"] = audio_file

        llm_call = await route_request(
            data=data,
            route_type="adiarize",
            llm_router=proxy_server_mod.llm_router,
            user_model=proxy_server_mod.user_model,
        )
        response = await llm_call

        hidden_params = (
            getattr(response, "_hidden_params", {}) or {}
            if not isinstance(response, dict)
            else response.get("_hidden_params", {}) or {}
        )
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

        asyncio.create_task(
            proxy_server_mod.proxy_logging_obj.update_request_status(
            litellm_call_id=data.get("litellm_call_id", ""), status="success"
        )
        )

        if isinstance(response, dict):
            response.pop("_hidden_params", None)

        return JSONResponse(content=response, headers=custom_headers)

    except Exception as e:
        await proxy_server_mod.proxy_logging_obj.post_call_failure_hook(
            user_api_key_dict=user_api_key_dict, original_exception=e, request_data=data
        )
        verbose_proxy_logger.error(
            "litellm.proxy.diarization_routes.diarize(): Exception occured - {}".format(str(e))
        )
        verbose_proxy_logger.debug(traceback.format_exc())
        raise e
