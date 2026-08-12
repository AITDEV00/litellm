import json

import httpx
import pytest

import litellm
from litellm.llms.custom_httpx.llm_http_handler import AsyncHTTPHandler

_BASE = "http://localhost:18080"


def _make_async_client(captured: dict) -> AsyncHTTPHandler:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        body = request.content
        if body:
            try:
                captured["body"] = json.loads(body)
            except Exception:
                captured["body"] = body.decode("utf-8", errors="replace")
        else:
            captured["body"] = None
        captured["headers"] = dict(request.headers)
        return httpx.Response(
            status_code=200,
            json={"status": "ok", "voices": []},
        )

    handler = AsyncHTTPHandler()
    handler.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    return handler


def _make_async_client_binary(captured: dict) -> AsyncHTTPHandler:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        body = request.content
        if body:
            try:
                captured["body"] = json.loads(body)
            except Exception:
                captured["body"] = body.decode("utf-8", errors="replace")
        else:
            captured["body"] = None
        return httpx.Response(
            status_code=200,
            content=b"\x49\x44\x33\x03\x00",
            headers={"content-type": "audio/mpeg"},
        )

    handler = AsyncHTTPHandler()
    handler.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    return handler


@pytest.mark.asyncio
async def test_acreate_voice_list_hits_correct_url_and_method():
    captured: dict = {}
    client = _make_async_client(captured)

    await litellm.acreate_voice(
        model="omnivoice/omni",
        voice_data={"action": "list"},
        api_base=_BASE,
        client=client,
    )

    assert captured["method"] == "GET"
    assert captured["url"] == _BASE + "/v1/voices"


@pytest.mark.asyncio
async def test_acreate_voice_list_profiles_hits_correct_url_and_method():
    captured: dict = {}
    client = _make_async_client(captured)

    await litellm.acreate_voice(
        model="omnivoice/omni",
        voice_data={"action": "list_profiles"},
        api_base=_BASE,
        client=client,
    )

    assert captured["method"] == "GET"
    assert captured["url"] == _BASE + "/v1/voices"


@pytest.mark.asyncio
async def test_acreate_voice_get_profile_hits_correct_url_and_method():
    captured: dict = {}
    client = _make_async_client(captured)

    await litellm.acreate_voice(
        model="omnivoice/omni",
        voice_data={"action": "get_profile", "profile_id": "Morgan"},
        api_base=_BASE,
        client=client,
    )

    assert captured["method"] == "GET"
    assert captured["url"] == _BASE + "/v1/voices/profiles/Morgan"


@pytest.mark.asyncio
async def test_acreate_voice_delete_profile_hits_correct_url_and_method():
    captured: dict = {}
    client = _make_async_client(captured)

    await litellm.acreate_voice(
        model="omnivoice/omni",
        voice_data={"action": "delete_profile", "profile_id": "Morgan"},
        api_base=_BASE,
        client=client,
    )

    assert captured["method"] == "DELETE"
    assert captured["url"] == _BASE + "/v1/voices/profiles/Morgan"


@pytest.mark.asyncio
async def test_acreate_voice_create_profile_sends_multipart_post():
    captured: dict = {}
    client = _make_async_client(captured)

    ref_audio = ("ref.wav", b"\x52\x49\x46\x46", "audio/wav")
    await litellm.acreate_voice(
        model="omnivoice/omni",
        voice_data={
            "action": "create_profile",
            "profile_id": "test_profile",
            "ref_audio": ref_audio,
            "ref_text": "hello",
        },
        api_base=_BASE,
        client=client,
    )

    assert captured["method"] == "POST"
    assert captured["url"] == _BASE + "/v1/voices/profiles"
    content_type = captured["headers"].get("content-type", "")
    assert "multipart/form-data" in content_type


@pytest.mark.asyncio
async def test_acreate_voice_update_profile_sends_patch():
    captured: dict = {}
    client = _make_async_client(captured)

    await litellm.acreate_voice(
        model="omnivoice/omni",
        voice_data={
            "action": "update_profile",
            "profile_id": "Morgan",
            "ref_text": "updated",
        },
        api_base=_BASE,
        client=client,
    )

    assert captured["method"] == "PATCH"
    assert captured["url"] == _BASE + "/v1/voices/profiles/Morgan"


@pytest.mark.asyncio
async def test_ascript_hits_correct_url_and_sends_json_body():
    captured: dict = {}
    client = _make_async_client_binary(captured)

    await litellm.ascript(
        model="omnivoice/omni",
        input="",
        voice=None,
        api_base=_BASE,
        client=client,
        script=[{"speaker": "S1", "text": "Hello"}],
        speakers=[{"speaker": "S1", "voice": "clone:test"}],
    )

    assert captured["method"] == "POST"
    assert captured["url"] == _BASE + "/v1/audio/script"
    assert isinstance(captured["body"], dict)
    assert captured["body"]["script"] == [{"speaker": "S1", "text": "Hello"}]
    assert captured["body"]["speakers"] == [{"speaker": "S1", "voice": "clone:test"}]


@pytest.mark.asyncio
async def test_acreate_voice_returns_parsed_json_response():
    captured: dict = {}
    client = _make_async_client(captured)

    result = await litellm.acreate_voice(
        model="omnivoice/omni",
        voice_data={"action": "list"},
        api_base=_BASE,
        client=client,
    )

    assert isinstance(result, dict)
    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_acreate_voice_returns_success_on_empty_204():
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=204, content=b"")

    handler = AsyncHTTPHandler()
    handler.client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))

    result = await litellm.acreate_voice(
        model="omnivoice/omni",
        voice_data={"action": "delete_profile", "profile_id": "Morgan"},
        api_base=_BASE,
        client=handler,
    )

    assert isinstance(result, dict)
    assert result["status"] == "success"
    assert result["status_code"] == 204


@pytest.mark.asyncio
async def test_router_acreate_voice_delegates_to_litellm_acreate_voice():
    from unittest.mock import AsyncMock, patch

    with patch("litellm.acreate_voice", new_callable=AsyncMock) as mock_acreate:
        mock_acreate.return_value = {"status": "ok"}
        from litellm import Router

        router = Router(
            model_list=[
                {
                    "model_name": "omnivoice",
                    "litellm_params": {
                        "model": "omnivoice/omni",
                        "api_base": _BASE,
                    },
                }
            ]
        )

        result = await router.acreate_voice(
            model="omnivoice",
            voice_data={"action": "list"},
        )

        assert result == {"status": "ok"}
        mock_acreate.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_request_voice_management_uses_acreate_voice():
    from unittest.mock import AsyncMock, patch

    with patch.object(
        litellm.Router, "acreate_voice", new_callable=AsyncMock
    ) as mock_acreate:
        mock_acreate.return_value = {"voices": []}
        from litellm import Router
        from litellm.proxy.route_llm_request import route_request

        router = Router(
            model_list=[
                {
                    "model_name": "omnivoice",
                    "litellm_params": {
                        "model": "omnivoice/omni",
                        "api_base": _BASE,
                    },
                }
            ]
        )

        result = await route_request(
            data={
                "model": "omnivoice",
                "voice_data": {"action": "list"},
            },
            route_type="acreate_voice",
            llm_router=router,
            user_model=None,
        )
        result = await result

        assert result == {"voices": []}
        mock_acreate.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_ascript_delegates_to_litellm_ascript():
    from unittest.mock import AsyncMock, patch

    with patch("litellm.ascript", new_callable=AsyncMock) as mock_ascript:
        mock_ascript.return_value = httpx.Response(
            status_code=200,
            content=b"\x49\x44\x33",
            headers={"content-type": "audio/mpeg"},
        )
        from litellm import Router

        router = Router(
            model_list=[
                {
                    "model_name": "omnivoice",
                    "litellm_params": {
                        "model": "omnivoice/omni",
                        "api_base": _BASE,
                    },
                }
            ]
        )

        await router.ascript(
            model="omnivoice",
            input="hello",
            voice=None,
        )

        mock_ascript.assert_awaited_once()
