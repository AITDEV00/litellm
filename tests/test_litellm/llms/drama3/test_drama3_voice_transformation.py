import pytest

from litellm.llms.drama3.voice.transformation import Drama3VoiceConfig


def _config() -> Drama3VoiceConfig:
    return Drama3VoiceConfig()


def test_drama3_voice_supported_actions():
    actions = Drama3VoiceConfig.SUPPORTED_VOICE_ACTIONS
    for a in ("register", "list", "delete_profile"):
        assert a in actions


def test_drama3_voice_declares_no_load_action():
    """The pod has no load endpoint (its registry is list/upload/delete only)
    and no gateway route produces a load action, so declaring it would make
    _voice_capable_providers pick this pod for a request it can only 404.
    BaseVoiceConfig ships load in its default set; this pins the override."""
    assert "load" not in Drama3VoiceConfig.SUPPORTED_VOICE_ACTIONS


def test_drama3_voice_list_url():
    url = _config().get_complete_url(
        model="fishaudio/drama-3",
        api_base="http://10.43.173.29:8080/v1",
        litellm_params={"voice_action": "list"},
    )
    assert url == "http://10.43.173.29:8080/v1/audio/voices"


def test_drama3_voice_register_url_ignores_action_suffix_paths():
    url = _config().get_complete_url(
        model="fishaudio/drama-3",
        api_base="http://10.43.173.29:8080/v1",
        litellm_params={"voice_action": "register"},
    )
    assert url == "http://10.43.173.29:8080/v1/audio/voices"


def test_drama3_voice_delete_url_requires_name():
    with pytest.raises(Exception, match="required for Drama 3 voice deletion"):
        _config().get_complete_url(
            model="fishaudio/drama-3",
            api_base="http://10.43.173.29:8080/v1",
            litellm_params={"voice_action": "delete_profile"},
        )


def test_drama3_voice_delete_url():
    url = _config().get_complete_url(
        model="fishaudio/drama-3",
        api_base="http://10.43.173.29:8080/v1",
        litellm_params={"voice_action": "delete_profile", "profile_id": "Narrator_01"},
    )
    assert url == "http://10.43.173.29:8080/v1/audio/voices/Narrator_01"


def test_drama3_voice_list_request_is_get():
    data = _config().transform_create_voice_request(
        model="fishaudio/drama-3",
        voice_data={},
        optional_params={},
        litellm_params={"voice_action": "list"},
        headers={},
    )
    assert data["method"] == "GET"


def test_drama3_voice_delete_request_is_delete():
    data = _config().transform_create_voice_request(
        model="fishaudio/drama-3",
        voice_data={},
        optional_params={},
        litellm_params={"voice_action": "delete_profile", "profile_id": "X"},
        headers={},
    )
    assert data["method"] == "DELETE"


def test_drama3_voice_register_requires_name():
    from litellm.llms.base_llm.chat.transformation import BaseLLMException

    with pytest.raises(BaseLLMException, match="'name' is required"):
        _config().transform_create_voice_request(
            model="fishaudio/drama-3",
            voice_data={"ref_audio": ("r.wav", b"\x00", "audio/wav")},
            optional_params={},
            litellm_params={"voice_action": "register"},
            headers={},
        )


def test_drama3_voice_register_requires_audio():
    from litellm.llms.base_llm.chat.transformation import BaseLLMException

    with pytest.raises(BaseLLMException, match="'ref_audio' is required"):
        _config().transform_create_voice_request(
            model="fishaudio/drama-3",
            voice_data={"name": "Narrator_01"},
            optional_params={},
            litellm_params={"voice_action": "register"},
            headers={},
        )


def test_drama3_voice_register_forwards_consent():
    """The pod's upload endpoint requires a consent string (Form(...)); the
    gateway's create_voice route only forwards keys listed in _VOICE_DATA_KEYS,
    so consent must be there or the register action can never carry it."""
    from litellm.proxy.voice_routes import _VOICE_DATA_KEYS

    assert "consent" in _VOICE_DATA_KEYS


def test_drama3_voice_register_builds_multipart():
    """The pod's upload contract is multipart: name, consent, ref_text plus an
    audio_sample file. This is the contract the old JSON-only path could never
    speak."""
    data = _config().transform_create_voice_request(
        model="fishaudio/drama-3",
        voice_data={
            "name": "Narrator_01",
            "consent": "I have permission to use this voice.",
            "ref_text": "A sample reference transcript.",
            "ref_audio": ("my_voice.wav", b"RIFFxxxx", "audio/wav"),
        },
        optional_params={},
        litellm_params={"voice_action": "register"},
        headers={},
    )
    form = data["form_data"]
    files = data["files"]
    assert form["name"] == "Narrator_01"
    assert form["consent"] == "I have permission to use this voice."
    assert form["ref_text"] == "A sample reference transcript."
    assert files["audio_sample"] == ("my_voice.wav", b"RIFFxxxx", "audio/wav")


def test_drama3_voice_register_from_bytes():
    data = _config().transform_create_voice_request(
        model="fishaudio/drama-3",
        voice_data={"name": "N2", "ref_audio": b"\x01\x02"},
        optional_params={},
        litellm_params={"voice_action": "register"},
        headers={},
    )
    filename, content, content_type = data["files"]["audio_sample"]
    assert filename == "ref_audio.wav"
    assert content == b"\x01\x02"
    assert content_type == "audio/wav"


def test_drama3_voice_response_passthrough():
    class _FakeResp:
        status_code = 200
        content = b'{"name": "Narrator_01"}'

        def json(self):
            return {"name": "Narrator_01"}

    result = _config().transform_create_voice_response(
        model="fishaudio/drama-3", raw_response=_FakeResp(), logging_obj=None
    )
    assert result == {"name": "Narrator_01"}


def test_drama3_voice_empty_response_is_success():
    class _FakeResp:
        status_code = 204
        content = b""

    result = _config().transform_create_voice_response(
        model="fishaudio/drama-3", raw_response=_FakeResp(), logging_obj=None
    )
    assert result["status"] == "success"


def test_create_voice_route_parses_multipart_registration():
    """The pod's voice upload is a multipart request, but the gateway's
    create_voice route parsed every body with orjson.loads, so a multipart
    registration 500'd before ever reaching this provider's transform.

    Drives the real route handler with a multipart-shaped request; the route's
    own dependencies (auth hooks, router) are stubbed only where the handler
    delegates. Before the fix this raised orjson.JSONDecodeError on the body.
    """
    import asyncio
    import types

    class _FormFile:
        filename = "my_voice.wav"
        content_type = "audio/wav"

        def __init__(self, content: bytes) -> None:
            self._content = content

        async def read(self) -> bytes:
            return self._content

    class _FakeRequest:
        headers = {"content-type": "multipart/form-data; boundary=x"}
        query_params = {}

        async def form(self):
            return {
                "name": "Narrator_01",
                "consent": "I have permission to use this voice.",
                "ref_audio": _FormFile(b"RIFFxxxx"),
            }

    captured: dict = {}

    class _Hooks:
        async def pre_call_hook(self, **kwargs):
            return kwargs["data"]

        async def post_call_failure_hook(self, **kwargs):
            return None

        async def update_request_status(self, **kwargs):
            return None

    fake_proxy_server = types.ModuleType("litellm.proxy.proxy_server")
    fake_proxy_server.general_settings = {}
    fake_proxy_server.version = "test"
    fake_proxy_server.proxy_config = None
    fake_proxy_server.user_model = None
    fake_proxy_server.llm_router = None
    fake_proxy_server.proxy_logging_obj = _Hooks()

    async def _fake_get_form_data(request):
        return await request.form()

    async def _fake_add_litellm_data(**kwargs):
        return kwargs["data"]

    async def _fake_route_request(**kwargs):
        """The route awaits route_request's result a second time (the real one
        resolves to the router's coroutine), so return a coroutine here."""
        captured.update(kwargs["data"])

        async def _response():
            return {"ok": True}

        return _response()

    class _FakeUserKey:
        user_id = "u1"

    mp = pytest.MonkeyPatch()
    mp.setattr("litellm.proxy.voice_routes.get_form_data", _fake_get_form_data)
    mp.setattr("litellm.proxy.voice_routes.add_litellm_data_to_request", _fake_add_litellm_data)
    mp.setattr("litellm.proxy.voice_routes.route_request", _fake_route_request)
    mp.setattr(
        "litellm.proxy.voice_routes.ProxyBaseLLMRequestProcessing.get_custom_headers",
        classmethod(lambda cls, **kwargs: {}),
    )
    mp.setitem(__import__("sys").modules, "litellm.proxy.proxy_server", fake_proxy_server)

    from litellm.proxy.voice_routes import create_voice

    async def run():
        return await create_voice(
            request=_FakeRequest(),
            fastapi_response=None,
            user_api_key_dict=_FakeUserKey(),
        )

    try:
        result = asyncio.run(run())
    finally:
        mp.undo()

    assert result is not None
    assert captured["voice_data"]["action"] == "register"
    assert captured["voice_data"]["name"] == "Narrator_01"
    assert captured["voice_data"]["consent"] == "I have permission to use this voice."
    assert captured["voice_data"]["ref_audio"] == ("my_voice.wav", b"RIFFxxxx", "audio/wav")
