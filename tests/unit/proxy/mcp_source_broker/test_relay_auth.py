import json
import re
from pathlib import Path

import httpx
import pytest

from litellm.proxy.mcp_source_broker.models import RelayUser, SourceCreate
from litellm.proxy.mcp_source_broker.relay_auth import (
    DuplicateUsername,
    authfile_content,
    refresh_authfile,
    run_authfile_loop,
)
from litellm.proxy.mcp_source_broker.store import SourceStore


async def relay_user(store: SourceStore, source: SourceCreate) -> RelayUser:
    enrolled = await store.enroll(source, source.owner, "key")
    claim = await store.claim(enrolled.enrollment_id, enrolled.token)
    assert claim is not None
    return (await store.users())[0]


@pytest.mark.asyncio
async def test_exact_escaped_authfile_and_private_mode(
    store: SourceStore, source: SourceCreate, tmp_path: Path
) -> None:
    user = await relay_user(store, source)
    path = tmp_path / "private" / "users.json"
    markup_user = user.model_copy(update={"relay_host": "127.0.0.20", "relay_port": 18080})
    await write_and_load(markup_user, path)
    record = json.loads(path.read_text())
    permission = f"{markup_user.username}:{markup_user.password}"
    assert record == {permission: ["^R:127\\.0\\.0\\.20:18080$"]}
    assert re.fullmatch(record[permission][0], "R:127.0.0.20:18080")
    assert re.fullmatch(record[permission][0], "xR:127.0.0.20:18080") is None
    assert re.fullmatch(record[permission][0], "R:127x0x0x20:18080") is None
    assert path.stat().st_mode & 0o777 == 0o600
    assert re.fullmatch(record[permission][0], "R:127[.]0[.]0[.]20:18080") is None


async def write_and_load(user: RelayUser, path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer service-token"
        return httpx.Response(200, json=[user.model_dump(mode="json")])

    async with httpx.AsyncClient(
        base_url="http://broker",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer service-token"},
    ) as client:
        await refresh_authfile(client, path)


@pytest.mark.asyncio
async def test_refresh_rejects_duplicate_username_and_retains_prior_file(
    store: SourceStore, source: SourceCreate, tmp_path: Path
) -> None:
    user = await relay_user(store, source)
    conflicting = user.model_copy(update={"password": "a-different-secret", "relay_host": "127.0.0.21"})
    with pytest.raises(DuplicateUsername):
        authfile_content((user, conflicting))
    path = tmp_path / "users.json"
    previous = b'{"existing-user:previous":[]}\n'
    path.write_bytes(previous)

    async with httpx.AsyncClient(
        base_url="http://broker",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=[user.model_dump(mode="json"), conflicting.model_dump(mode="json")]
            )
        ),
    ) as client:
        with pytest.raises(DuplicateUsername):
            await refresh_authfile(client, path)
    assert path.read_bytes() == previous

    async with httpx.AsyncClient(
        base_url="http://broker", transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await refresh_authfile(client, path)
    assert path.read_bytes() == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "::1", "r(?).*", "192.168.1.20"])
async def test_unsafe_listeners_rejected_before_write(
    store: SourceStore, source: SourceCreate, tmp_path: Path, host: str
) -> None:
    user = (await relay_user(store, source)).model_copy(update={"relay_host": host})
    path = tmp_path / "users.json"
    path.write_bytes(b"prior")
    with pytest.raises(ValueError, match=r"relay listeners|IPv4 or IPv6"):
        authfile_content((user,))
    assert path.read_bytes() == b"prior"


@pytest.mark.asyncio
async def test_authfile_loop_retains_valid_file_when_control_is_unavailable(
    store: SourceStore, source: SourceCreate, tmp_path: Path
) -> None:
    user = await relay_user(store, source)
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(200, json=[user.model_dump(mode="json")])
        raise httpx.ConnectError("offline", request=request)

    class StopLoop(Exception):
        pass

    path = tmp_path / "users.json"
    sleeps = 0

    async def sleep(delay: float) -> None:
        nonlocal sleeps
        assert delay == 0.01
        sleeps += 1
        if sleeps == 2:
            raise StopLoop

    with pytest.raises(StopLoop):
        await run_authfile_loop(
            path,
            broker_url="http://broker",
            service_token="service-token",
            transport=httpx.MockTransport(handler),
            interval_seconds=0.01,
            sleep=sleep,
        )
    expected = authfile_content((user,))
    assert path.read_bytes() == expected
    assert path.stat().st_mode & 0o777 == 0o600
