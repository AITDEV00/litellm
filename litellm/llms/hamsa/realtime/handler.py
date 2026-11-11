from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import (  # noqa: TID251  # existing polymorphic realtime payloads and logger interfaces
    TYPE_CHECKING,
    Any,
    Final,
)

from litellm._logging import verbose_logger
from litellm.llms.base_llm.realtime.transformation import BaseRealtimeConfig
from litellm.llms.hamsa.common_utils import HamsaModelInfo
from litellm.types.realtime import (
    RealtimeResponseTransformInput,
    RealtimeResponseTypedDict,
)

if TYPE_CHECKING:
    from websockets.asyncio.connection import ClientConnection


class HamsaRealtimeConfig(HamsaModelInfo, BaseRealtimeConfig):
    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: str | None = None,
    ) -> dict:
        return self._inject_auth_headers(headers, api_key)

    def get_complete_url(self, api_base: str | None, model: str, api_key: str | None = None) -> str:
        base: Final = self._resolve_base(api_base)
        ws_base: Final = base.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return ws_base + "/ws"

    @staticmethod
    def upstream_handshake(message: str, api_key: str) -> str:
        try:
            parsed: Final = json.loads(message)
        except json.JSONDecodeError:
            return message
        if not isinstance(parsed, dict) or parsed.get("type") != "handshake":
            return message
        return json.dumps({**parsed, "api_key": api_key})

    def transform_realtime_request(
        self,
        message: str,
        model: str,
        session_configuration_request: str | None = None,
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
    api_base: str | None = None,
    api_key: str | None = None,
    logging_obj: Any = None,
    connect: Callable[..., AbstractAsyncContextManager[ClientConnection]] | None = None,
    **kwargs: Any,  # kwargs-ok: the provider-independent realtime dispatch includes unrelated provider parameters
) -> None:
    from websockets.asyncio.client import connect as default_connect

    config: Final = HamsaRealtimeConfig()
    open_connection: Final = connect or default_connect
    url: Final = config.get_complete_url(api_base, model, api_key)
    upstream_api_key: Final = config.get_api_key(api_key)

    if upstream_api_key is None:
        from litellm.llms.base_llm.chat.transformation import BaseLLMException

        raise BaseLLMException(
            status_code=401,
            message="Missing Hamsa API key for realtime. Set HAMSA_API_KEY or pass api_key in model config.",
            headers={},
        )

    verbose_logger.info(f"Hamsa realtime: connecting to {url}")

    async with open_connection(url, max_size=50 * 1024 * 1024) as backend_ws:
        verbose_logger.info("Hamsa realtime: upstream connection established")

        async def forward_client_to_backend() -> None:
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
                        upstream_text: Final = config.upstream_handshake(text_data, upstream_api_key)
                        await backend_ws.send(upstream_text)
                        if upstream_text != text_data:
                            verbose_logger.info(
                                "Hamsa realtime: injected the configured upstream credential into the handshake"
                            )
                    elif bytes_data is not None:
                        await backend_ws.send(bytes_data)
            except asyncio.CancelledError:
                raise
            except Exception:
                verbose_logger.exception("Hamsa realtime: error forwarding client->backend")
                await backend_ws.close()

        async def forward_backend_to_client() -> None:
            from websockets.exceptions import ConnectionClosed

            try:
                while True:
                    message = await backend_ws.recv()
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)
            except ConnectionClosed as close:
                if close.code != 1000:
                    verbose_logger.warning("Hamsa realtime backend closed: %s", close)
            except Exception:
                verbose_logger.exception("Hamsa realtime: error forwarding backend->client")
            finally:
                await backend_ws.close()

        client_forward: Final = asyncio.create_task(forward_client_to_backend())
        backend_forward: Final = asyncio.create_task(forward_backend_to_client())
        try:
            _, pending = await asyncio.wait(
                (client_forward, backend_forward),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            client_forward.cancel()
            backend_forward.cancel()
            await asyncio.gather(client_forward, backend_forward, return_exceptions=True)
            await websocket.close()
