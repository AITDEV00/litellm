import asyncio
import json
import socket
from contextlib import closing
from typing import Any, Final

import pytest
import websockets

from litellm.llms.hamsa.realtime.handler import HamsaRealtimeConfig, hamsa_realtime


@pytest.mark.parametrize(
    ("message", "expected"),
    (
        ({"type": "handshake", "api_key": "browser-value"}, "configured-upstream"),
        ({"type": "handshake", "options": {}}, "configured-upstream"),
        ({"type": "handshake", "api_key": "other-client-value"}, "configured-upstream"),
        ({"type": "audio", "api_key": "browser-value"}, "browser-value"),
    ),
)
def test_only_the_upstream_handshake_injects_the_configured_credential(message: dict[str, Any], expected: str) -> None:
    transformed = HamsaRealtimeConfig.upstream_handshake(json.dumps(message), "configured-upstream")
    assert json.loads(transformed).get("api_key") == expected


def test_non_json_realtime_messages_pass_unchanged() -> None:
    assert HamsaRealtimeConfig.upstream_handshake("non-json", "configured-upstream") == "non-json"


class _ProxyWebSocket:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = [
            {"type": "websocket.receive", "text": json.dumps({"type": "handshake", "api_key": "browser-value"})},
            {"type": "websocket.receive", "bytes": b"\x00\x01\xff\xfe"},
        ]
        self.responses: list[dict[str, Any]] = []
        self.read_lock = asyncio.Lock()
        self.disconnected = False
        self.closed = False

    async def receive(self) -> dict[str, Any]:
        async with self.read_lock:
            if self.received:
                return self.received.pop(0)
        if not self.disconnected:
            self.disconnected = True
            await asyncio.sleep(0.02)
        return {"type": "websocket.disconnect"}

    async def send_text(self, value: str) -> None:
        self.responses.append(json.loads(value))

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_realtime_injects_upstream_credential_and_streams_pcm() -> None:
    async def backend(connection: Any) -> None:
        handshake = json.loads(await connection.recv(decode=True))
        assert handshake["api_key"] == "configured-upstream"
        await connection.send(json.dumps({"type": "handshake_ack"}))
        assert await connection.recv(decode=False) == b"\x00\x01\xff\xfe"
        await connection.send(json.dumps({"type": "transcription", "data": {"transcription": "Hello"}}))
        await connection.close()

    host = "127.0.0.1"
    with closing(socket.socket()) as sock:
        sock.bind((host, 0))
        port = sock.getsockname()[1]
    websocket = _ProxyWebSocket()
    async with websockets.serve(backend, host, port):
        await asyncio.wait_for(
            hamsa_realtime(
                model="hamsa-stt",
                websocket=websocket,
                api_base=f"http://{host}:{port}",
                api_key="configured-upstream",
            ),
            timeout=1,
        )

    assert websocket.responses[0]["type"] == "handshake_ack"
    assert (
        next(message for message in websocket.responses if message["type"] == "transcription")["data"]["transcription"]
        == "Hello"
    )
    assert websocket.closed


class _IdleProxyWebSocket(_ProxyWebSocket):
    async def receive(self) -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_realtime_closes_when_upstream_finishes_before_the_client() -> None:
    async def backend(connection: Any) -> None:
        await connection.send(json.dumps({"type": "handshake_ack"}))
        await connection.close()

    host = "127.0.0.1"
    with closing(socket.socket()) as sock:
        sock.bind((host, 0))
        port = sock.getsockname()[1]
    websocket = _IdleProxyWebSocket()
    async with websockets.serve(backend, host, port):
        await asyncio.wait_for(
            hamsa_realtime(
                model="hamsa-stt", websocket=websocket, api_base=f"http://{host}:{port}", api_key="configured-upstream"
            ),
            timeout=1,
        )

    assert websocket.responses == [{"type": "handshake_ack"}]
    assert websocket.closed


@pytest.mark.asyncio
@pytest.mark.timeout(3)
@pytest.mark.parametrize("shutdown", ["rejected", "abrupt", "client_disconnect"])
async def test_realtime_shutdown_does_not_leave_the_peer_forwarder_hanging(shutdown: str) -> None:
    upstream_closed = asyncio.Event()

    async def backend(connection: Any) -> None:
        try:
            if shutdown == "rejected":
                await connection.close(code=1008, reason="Invalid upstream credential")
            elif shutdown == "abrupt":
                connection.transport.abort()
                await connection.wait_closed()
            else:
                handshake = json.loads(await connection.recv(decode=True))
                assert handshake["api_key"] == "configured-upstream"
                await connection.close(code=1000)
                await connection.wait_closed()
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            upstream_closed.set()

    host = "127.0.0.1"
    with closing(socket.socket()) as sock:
        sock.bind((host, 0))
        port = sock.getsockname()[1]
    websocket: Final = _IdleProxyWebSocket()
    if shutdown == "client_disconnect":
        websocket.received = [{"type": "websocket.receive", "text": json.dumps({"type": "handshake"})}]
        disconnected = asyncio.Event()

        async def receive() -> dict[str, Any]:
            if websocket.received:
                return websocket.received.pop(0)
            disconnected.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        websocket.disconnect_event = disconnected
    else:

        async def receive() -> dict[str, Any]:
            return await _IdleProxyWebSocket.receive(websocket)

    websocket.receive = receive

    async with websockets.serve(backend, host, port):
        relay = asyncio.create_task(
            hamsa_realtime(
                model="hamsa-stt",
                websocket=websocket,
                api_base=f"http://{host}:{port}",
                api_key="configured-upstream",
            )
        )
        if shutdown == "client_disconnect":
            await asyncio.wait_for(websocket.disconnect_event.wait(), timeout=1)
            assert not relay.done()
            relay.cancel()
            with pytest.raises(asyncio.CancelledError):
                await relay
        else:
            await asyncio.wait_for(relay, timeout=1)

    await asyncio.wait_for(upstream_closed.wait(), timeout=1)
    assert websocket.closed
