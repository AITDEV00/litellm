import asyncio
import json
import logging
from typing import Any, Optional

from litellm._logging import verbose_logger
from litellm.llms.base_llm.realtime.transformation import BaseRealtimeConfig
from litellm.llms.hamsa.common_utils import HamsaModelInfo
from litellm.types.realtime import (
    RealtimeResponseTransformInput,
    RealtimeResponseTypedDict,
)

logger = logging.getLogger(__name__)


class HamsaRealtimeConfig(HamsaModelInfo, BaseRealtimeConfig):
    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: Optional[str] = None,
    ) -> dict:
        return headers

    def get_complete_url(self, api_base: Optional[str], model: str, api_key: Optional[str] = None) -> str:
        base = self.get_api_base(api_base)
        if base is None:
            raise ValueError("Missing Hamsa API base for realtime. Set HAMSA_API_BASE or pass api_base in model config.")
        base = base.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        return base + "/ws"

    def transform_realtime_request(
        self,
        message: str,
        model: str,
        session_configuration_request: Optional[str] = None,
    ) -> list[str]:
        return [message]

    def transform_realtime_response(
        self,
        message: str | bytes,
        model: str,
        logging_obj: Any,
        realtime_response_transform_input: RealtimeResponseTransformInput,
    ) -> RealtimeResponseTypedDict:
        if isinstance(message, bytes):
            message = message.decode("utf-8", errors="replace")
        try:
            event: dict = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            event = {"type": "raw", "data": message}

        return {
            "response": [event],
            "current_output_item_id": realtime_response_transform_input.get("current_output_item_id"),
            "current_response_id": realtime_response_transform_input.get("current_response_id"),
            "current_delta_chunks": realtime_response_transform_input.get("current_delta_chunks"),
            "current_conversation_id": realtime_response_transform_input.get("current_conversation_id"),
            "current_item_chunks": realtime_response_transform_input.get("current_item_chunks"),
            "current_delta_type": realtime_response_transform_input.get("current_delta_type"),
            "session_configuration_request": realtime_response_transform_input.get("session_configuration_request"),
        }


async def hamsa_realtime(
    model: str,
    websocket: Any,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    logging_obj: Any = None,
    **kwargs: Any,
) -> None:
    from websockets.asyncio.client import connect

    config = HamsaRealtimeConfig()
    url = config.get_complete_url(api_base, model, api_key)
    upstream_api_key = config.get_api_key(api_key)

    if upstream_api_key is None:
        raise ValueError("Missing Hamsa API key for realtime. Set HAMSA_API_KEY or pass api_key in model config.")

    verbose_logger.info(f"Hamsa realtime: connecting to {url}")

    async with connect(url, max_size=50 * 1024 * 1024) as backend_ws:
        verbose_logger.info("Hamsa realtime: upstream connection established")

        async def forward_client_to_backend() -> None:
            handshake_done = False
            try:
                while True:
                    message = await websocket.receive()
                    msg_type = message.get("type")
                    if msg_type == "websocket.disconnect":
                        await backend_ws.close()
                        break
                    text_data = message.get("text")
                    bytes_data = message.get("bytes")
                    if text_data is not None:
                        if not handshake_done:
                            try:
                                parsed = json.loads(text_data)
                                if isinstance(parsed, dict) and parsed.get("type") == "handshake":
                                    parsed["api_key"] = upstream_api_key
                                    text_data = json.dumps(parsed)
                                    verbose_logger.info("Hamsa realtime: injected upstream api_key into handshake")
                            except (json.JSONDecodeError, TypeError):
                                pass
                            handshake_done = True
                        await backend_ws.send(text_data)
                    elif bytes_data is not None:
                        await backend_ws.send(bytes_data)
            except asyncio.CancelledError:
                raise
            except Exception:
                verbose_logger.exception("Hamsa realtime: error forwarding client->backend")
                await backend_ws.close()

        async def forward_backend_to_client() -> None:
            try:
                while True:
                    message = await backend_ws.recv()
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                verbose_logger.exception("Hamsa realtime: error forwarding backend->client")
                try:
                    await websocket.close()
                except Exception:
                    pass

        await asyncio.gather(
            forward_client_to_backend(),
            forward_backend_to_client(),
        )
