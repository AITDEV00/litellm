from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
import ssl
import re
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

logger = logging.getLogger("oicm-stt")


def _hamsa_api_key() -> str:
    return os.environ.get(
        "HAMSA_STT_UPSTREAM_API_KEY",
        "gAAAAABo-1oxslqx1hGc8nGn_7iWiD0jwAGE7tDk3MgA-t_9gM05qFZIP1tTiBgJpDkTaTrf7OHe9RLj2AjspUYuKxAqVnPjIJ6AD6q-0E8paCMBreZ8pGc=",
    )


def _ws_to_http(url: str) -> str:
    if url.startswith("ws://"):
        return "http://" + url[5:]
    if url.startswith("wss://"):
        return "https://" + url[6:]
    return url


def _hamsa_rest_url(api_base: str) -> str:
    m = re.search(r"/models/([0-9a-f-]{36})/", api_base)
    if m:
        model_id = m.group(1)
        return f"http://s-{model_id}.adeo.svc.cluster.local:8080/transcribe"
    base = _ws_to_http(api_base.rstrip("/"))
    if base.endswith("/ws/ws"):
        base = base[: -len("/ws/ws")] + "/proxy"
    return base + "/transcribe"


def _hamsa_ws_url(api_base: str) -> str:
    m = re.search(r"/models/([0-9a-f-]{36})/", api_base)
    if m:
        model_id = m.group(1)
        return f"ws://s-{model_id}.adeo.svc.cluster.local:8080/ws"
    return api_base.rstrip("/")


async def hamsa_transcribe(
    body: dict[str, Any],
    api_base: str,
) -> dict[str, Any]:
    url = _hamsa_rest_url(api_base)
    body_bytes = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "x-api-key": _hamsa_api_key(),
        },
        method="POST",
    )

    loop = asyncio.get_event_loop()

    def _do_request() -> bytes:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            return resp.read()

    raw = await loop.run_in_executor(None, _do_request)
    return json.loads(raw)


async def hamsa_handle_websocket(
    websocket: WebSocket,
    api_base: str,
) -> None:
    from websockets.asyncio.client import connect

    ws_target = _hamsa_ws_url(api_base)
    api_key = _hamsa_api_key()

    logger.info("Hamsa WS: client connected, forwarding to %s", ws_target)

    try:
        connect_kwargs: dict[str, Any] = {}
        if ws_target.startswith("wss://"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            connect_kwargs["ssl"] = ctx
        async with connect(ws_target, **connect_kwargs) as upstream_ws:
            logger.info("Hamsa WS: upstream connection established")

            async def forward_client_to_upstream() -> None:
                handshake_done = False
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
                            if not handshake_done:
                                try:
                                    parsed = json.loads(text_data)
                                    if parsed.get("type") == "handshake":
                                        parsed["api_key"] = api_key
                                        text_data = json.dumps(parsed)
                                        logger.info(
                                            "Hamsa WS: injected upstream api_key into handshake"
                                        )
                                except (json.JSONDecodeError, TypeError):
                                    pass
                                handshake_done = True
                            await upstream_ws.send(text_data)
                        elif bytes_data is not None:
                            await upstream_ws.send(bytes_data)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Hamsa WS: error forwarding client->upstream")
                    await upstream_ws.close()

            async def forward_upstream_to_client() -> None:
                try:
                    while True:
                        message = await upstream_ws.recv()
                        if websocket.client_state == WebSocketState.DISCONNECTED:
                            break
                        if isinstance(message, str):
                            await websocket.send_text(message)
                        else:
                            await websocket.send_bytes(message)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Hamsa WS: error forwarding upstream->client")
                    await websocket.close()

            await asyncio.gather(
                forward_client_to_upstream(),
                forward_upstream_to_client(),
            )
    except Exception as e:
        logger.error("Hamsa WS: upstream connection failed: %s", e)
        if websocket.client_state != WebSocketState.DISCONNECTED:
            await websocket.close(code=1011, reason=str(e))
