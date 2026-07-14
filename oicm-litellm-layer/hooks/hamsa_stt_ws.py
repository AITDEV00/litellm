import asyncio
import json
import logging
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from fastapi import WebSocket, status
from starlette.websockets import WebSocketState

logger = logging.getLogger("oicm-hamsa-ws")


async def register_hamsa_stt_websocket_route():
    from litellm.proxy.proxy_server import app, llm_router

    target = "wss://inference.adeoaiengine.ecouncil.ae/models/9c57bce9-0583-4bf7-9443-08825220a231/ws/ws"

    upstream_api_key = "gAAAAABo-1oxslqx1hGc8nGn_7iWiD0jwAGE7tDk3MgA-t_9gM05qFZIP1tTiBgJpDkTaTrf7OHe9RLj2AjspUYuKxAqVnPjIJ6AD6q-0E8paCMBreZ8pGc="
    upstream_bearer = "sk-ZWPlJ59ypArMEWcpq_UmL_-Tw5EEzSPL2_y5zy7QimM"

    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth_websocket

    async def hamsa_stt_ws_endpoint(
        websocket: WebSocket,
    ):
        ws_scope = websocket.scope or {}
        scope_headers = list(ws_scope.get("headers") or [])
        synthetic_scope: Dict[str, Any] = {
            "type": "http",
            "headers": scope_headers,
            "path": ws_scope.get("path", ""),
        }
        for key in ("root_path", "app_root_path"):
            if key in ws_scope:
                synthetic_scope[key] = ws_scope[key]

        from starlette.requests import Request

        request = Request(scope=synthetic_scope)
        request._url = websocket.url
        query_params = websocket.query_params

        async def return_body():
            return b""

        request.body = return_body  # type: ignore

        authorization = websocket.headers.get("authorization")
        if not authorization:
            api_key = websocket.headers.get("api-key")
            if not api_key:
                api_key = query_params.get("authorization") or query_params.get("api_key")
                if api_key and api_key.startswith("Bearer "):
                    api_key = api_key[len("Bearer "):]
                if not api_key:
                    await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
                    return
                authorization = f"Bearer {api_key}"
            else:
                authorization = f"Bearer {api_key}"
        elif not authorization.startswith("Bearer "):
            authorization = f"Bearer {authorization}"

        from litellm.proxy.auth.user_api_key_auth import user_api_key_auth

        try:
            user_api_key_dict = await user_api_key_auth(
                request=request, api_key=authorization
            )
        except Exception as e:
            logger.error(f"Hamsa STT WS auth failed: {e}")
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await websocket.accept()
        logger.info(
            f"Hamsa STT WS: client connected, forwarding to {target}"
        )

        from websockets.asyncio.client import connect

        upstream_headers = {
            "Authorization": f"Bearer {upstream_bearer}",
            "X-API-KEY": upstream_api_key,
        }

        try:
            async with connect(target, additional_headers=upstream_headers) as upstream_ws:
                logger.info("Hamsa STT WS: upstream connection established")

                async def forward_client_to_upstream():
                    try:
                        while True:
                            message = await websocket.receive()
                            msg_type = message.get("type")
                            if msg_type == "websocket.disconnect":
                                await upstream_ws.close()
                                break
                            text_data = message.get("text")
                            bytes_data = message.get("bytes")
                            if text_data is not None:
                                await upstream_ws.send(text_data)
                            elif bytes_data is not None:
                                await upstream_ws.send(bytes_data)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Hamsa STT WS: error forwarding client->upstream")
                        await upstream_ws.close()

                async def forward_upstream_to_client():
                    try:
                        while True:
                            raw = await upstream_ws.recv(decode=False)
                            if isinstance(raw, str):
                                raw = raw.encode("utf-8")
                            if websocket.client_state == WebSocketState.DISCONNECTED:
                                break
                            await websocket.send_bytes(raw)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception("Hamsa STT WS: error forwarding upstream->client")
                        await websocket.close()

                await asyncio.gather(
                    forward_client_to_upstream(),
                    forward_upstream_to_client(),
                )
        except Exception as e:
            logger.error(f"Hamsa STT WS: upstream connection failed: {e}")
            if websocket.client_state != WebSocketState.DISCONNECTED:
                await websocket.close(code=1011, reason=str(e))

    route_path = "/v1/hamsa/stt/ws"

    existing_paths = {r.path for r in app.routes if hasattr(r, "path")}
    if route_path not in existing_paths:
        app.websocket(route_path)(hamsa_stt_ws_endpoint)
        logger.info(f"Registered Hamsa STT WebSocket route at {route_path}")
    else:
        logger.info(f"Hamsa STT WebSocket route already registered at {route_path}")
