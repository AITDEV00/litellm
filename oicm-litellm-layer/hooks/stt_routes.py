from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, Optional

from fastapi import (
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    status,
)
from starlette.websockets import WebSocketState

logger = logging.getLogger("oicm-stt-routes")

_REST_PATH = "/custom/audio/transcriptions"
_WS_PATH = "/custom/realtime"


async def register_stt_routes() -> None:
    """Startup hook: register custom STT routes on the LiteLLM proxy app.

    Registers:
      POST /custom/audio/transcriptions  (REST, model from JSON body)
      WS   /custom/realtime              (model from ?model= query param)

    Both routes authenticate via LiteLLM's user_api_key_auth and resolve
    the model deployment through llm_router.get_available_deployment_for_pass_through(),
    which applies native RPM/TPM/priority/cooldown/load-balancing exactly
    like any other LiteLLM endpoint. The deployment's litellm_params.api_base
    is then passed to the Hamsa provider functions.
    """
    from litellm.proxy.auth.user_api_key_auth import (
        user_api_key_auth,
        user_api_key_auth_websocket,
    )
    from litellm.proxy.proxy_server import (
        app,
        general_settings,
        proxy_logging_obj,
        proxy_config,
        user_model,
        version,
    )

    existing_paths = {r.path for r in app.routes if hasattr(r, "path")}

    if _REST_PATH not in existing_paths:
        app.post(_REST_PATH)(
            _create_rest_endpoint(
                user_api_key_auth,
                proxy_logging_obj,
                general_settings,
                proxy_config,
                user_model,
                version,
            )
        )
        logger.info("Registered STT REST route at %s", _REST_PATH)
    else:
        logger.info("STT REST route already registered at %s", _REST_PATH)

    if _WS_PATH not in existing_paths:
        app.websocket(_WS_PATH)(
            _create_ws_endpoint(
                user_api_key_auth_websocket,
                proxy_logging_obj,
                general_settings,
                proxy_config,
                user_model,
                version,
            )
        )
        logger.info("Registered STT WebSocket route at %s", _WS_PATH)
    else:
        logger.info("STT WebSocket route already registered at %s", _WS_PATH)


def _resolve_deployment(model_name: str, llm_router: Any) -> dict:
    """Call the router's pass-through deployment selector.

    This applies native RPM/TPM/priority/cooldown/load-balancing checks.
    Returns the deployment dict (with 'litellm_params' containing 'api_base').
    Raises RouterRateLimitError if rate limits are exceeded.
    """
    deployment = llm_router.get_available_deployment_for_pass_through(
        model=model_name
    )
    if not deployment:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No deployment found for model '{model_name}' with use_in_pass_through=true",
        )
    return deployment


def _create_rest_endpoint(
    user_api_key_auth_dep: Any,
    proxy_logging_obj: Any,
    general_settings: dict,
    proxy_config: Any,
    user_model: Optional[str],
    version: Optional[str],
):
    async def _stt_transcriptions(
        request: Request,
        fastapi_response: Response,
        user_api_key_dict: Any = Depends(user_api_key_auth_dep),
    ):
        """POST /custom/audio/transcriptions

        Accepts a JSON body with 'model', 'audio' (base64), and provider
        options (lang, eos_enabled, eos_threshold, gender_detection, etc.).
        Resolves the model deployment through LiteLLM's router (native
        RPM/TPM/priority enforcement), then forwards to the provider.
        """
        from litellm.proxy.common_request_processing import (
            ProxyBaseLLMRequestProcessing,
        )
        from litellm.proxy.litellm_pre_call_utils import (
            add_litellm_data_to_request,
        )
        from litellm.proxy.proxy_server import llm_router
        from litellm.types.router import RouterRateLimitError

        from litellm_hooks.stt_provider import hamsa_transcribe

        raw_body = await request.json()

        provider_fields = (
            "audio",
            "prompt",
            "lang",
            "eos_enabled",
            "eos_threshold",
            "gender_detection",
            "speaker_identification",
            "wake_word",
            "threshold",
        )
        provider_body: Dict[str, Any] = {
            k: v for k, v in raw_body.items() if k in provider_fields
        }

        data: Dict[str, Any] = {
            k: v for k, v in raw_body.items() if k not in provider_fields
        }

        data = await add_litellm_data_to_request(
            data=data,
            request=request,
            user_api_key_dict=user_api_key_dict,
            proxy_config=proxy_config,
            general_settings=general_settings,
            version=version,
        )

        if data.get("user", None) is None and user_api_key_dict.user_id is not None:
            data["user"] = user_api_key_dict.user_id

        data["model"] = user_model or data.get("model", None)

        model_name = data.get("model")
        if not model_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="model field is required",
            )

        data["litellm_call_id"] = request.headers.get(
            "x-litellm-call-id", str(uuid.uuid4())
        )

        try:
            data = await proxy_logging_obj.pre_call_hook(
                user_api_key_dict=user_api_key_dict,
                data=data,
                call_type="transcription",
            )

            deployment = _resolve_deployment(model_name, llm_router)
            api_base = deployment.get("litellm_params", {}).get("api_base", "")

            result = await hamsa_transcribe(
                body=provider_body,
                api_base=api_base,
            )

            await proxy_logging_obj.update_request_status(
                litellm_call_id=data.get("litellm_call_id", ""),
                status="success",
            )

            fastapi_response.headers.update(
                ProxyBaseLLMRequestProcessing.get_custom_headers(
                    user_api_key_dict=user_api_key_dict,
                    model_id="",
                    cache_key="",
                    api_base="",
                    version=version or "",
                    request_data=data,
                    hidden_params={},
                )
            )

            return result

        except HTTPException:
            raise
        except RouterRateLimitError as e:
            await proxy_logging_obj.post_call_failure_hook(
                user_api_key_dict=user_api_key_dict,
                original_exception=e,
                request_data=data,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=str(e),
                headers={"retry-after": str(int(e.cooldown_time) + 1)},
            )
        except Exception as e:
            await proxy_logging_obj.post_call_failure_hook(
                user_api_key_dict=user_api_key_dict,
                original_exception=e,
                request_data=data,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"STT provider error: {e}",
            )

    _stt_transcriptions.__name__ = "stt_transcriptions"
    return _stt_transcriptions


def _create_ws_endpoint(
    user_api_key_auth_websocket_dep: Any,
    proxy_logging_obj: Any,
    general_settings: dict,
    proxy_config: Any,
    user_model: Optional[str],
    version: Optional[str],
):
    async def _stt_realtime(
        websocket: WebSocket,
        user_api_key_dict: Any = Depends(user_api_key_auth_websocket_dep),
    ):
        """WS /custom/realtime?model=<model_name>

        Authenticates via user_api_key_auth_websocket, runs pre-call hooks
        at connection time (same pattern as LiteLLM's /v1/realtime), then
        resolves the model deployment through the router and delegates to
        the Hamsa WS handler.
        """
        from litellm.proxy.auth.auth_checks import can_key_call_resolved_model
        from litellm.proxy.common_request_processing import (
            ProxyBaseLLMRequestProcessing,
        )
        from litellm.proxy.common_utils.realtime_utils import (
            _realtime_request_body,
        )
        from litellm.proxy.proxy_server import (
            REALTIME_REQUEST_SCOPE_TEMPLATE,
            llm_model_list,
            llm_router,
        )
        from litellm.types.router import RouterRateLimitError

        from litellm_hooks.stt_provider import hamsa_handle_websocket

        model_name = websocket.query_params.get("model")
        if not model_name:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason="model query parameter is required",
            )
            return

        try:
            await can_key_call_resolved_model(
                model=model_name,
                llm_model_list=llm_model_list,
                valid_token=user_api_key_dict,
                llm_router=llm_router,
            )
        except Exception as e:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=str(e)[:120],
            )
            return

        requested_protocols = [
            p.strip()
            for p in (websocket.headers.get("sec-websocket-protocol") or "").split(",")
            if p.strip()
        ]
        accept_kwargs: dict = {}
        if requested_protocols:
            accept_kwargs["subprotocol"] = requested_protocols[0]

        await websocket.accept(**accept_kwargs)

        data: Dict[str, Any] = {
            "model": model_name,
            "websocket": websocket,
            "query_params": {"model": model_name},
        }

        headers_list = list(websocket.scope.get("headers") or [])
        scope = REALTIME_REQUEST_SCOPE_TEMPLATE.copy()
        scope["headers"] = headers_list

        request = Request(scope=scope)
        request._url = websocket.url

        async def _return_body():
            return _realtime_request_body(model_name)

        request.body = _return_body  # type: ignore

        base_processor = ProxyBaseLLMRequestProcessing(data=data)

        try:
            data, _logging_obj = await base_processor.common_processing_pre_call_logic(
                request=request,
                general_settings=general_settings,
                user_api_key_dict=user_api_key_dict,
                proxy_logging_obj=proxy_logging_obj,
                proxy_config=proxy_config,
                version=version,
                user_model=user_model,
                route_type="_arealtime",
            )
        except Exception as e:
            logger.exception("STT WS pre-call error")
            try:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "pre_call_error",
                                "message": str(e),
                            },
                        }
                    )
                )
            except Exception:
                pass
            await websocket.close(code=1011, reason="Pre-call error")
            return

        try:
            deployment = _resolve_deployment(model_name, llm_router)
            api_base = deployment.get("litellm_params", {}).get("api_base", "")

            await hamsa_handle_websocket(
                websocket=websocket,
                api_base=api_base,
            )
        except RouterRateLimitError as e:
            logger.warning("STT WS rate limited: %s", e)
            try:
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "rate_limit_error",
                                "message": str(e),
                            },
                        }
                    )
                )
            except Exception:
                pass
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close(code=1013, reason="Rate limited")
        except Exception as e:
            logger.exception("STT WS provider error")
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close(code=1011, reason=str(e)[:120])

    _stt_realtime.__name__ = "stt_realtime"
    return _stt_realtime
