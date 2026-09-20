import httpx

from litellm.llms.omnivoice.script.transformation import OmniVoiceScriptConfig


_BASE = "http://localhost:18080"


def _config() -> OmniVoiceScriptConfig:
    return OmniVoiceScriptConfig()


def test_script_url():
    url = _config().get_complete_url(
        model="omnivoice",
        api_base=_BASE,
        litellm_params={},
    )
    assert url == _BASE + "/v1/audio/script"


def test_script_url_strips_trailing_v1():
    url = _config().get_complete_url(
        model="omnivoice",
        api_base=_BASE + "/v1",
        litellm_params={},
    )
    assert url == _BASE + "/v1/audio/script"


def test_script_url_requires_api_base():
    try:
        _config().get_complete_url(model="omnivoice", api_base=None, litellm_params={})
        assert False, "Should have raised for missing api_base"
    except Exception as e:
        assert "Missing OmniVoice API base" in str(e)


def test_script_map_openai_params_extracts_script_and_speakers():
    voice, mapped = _config().map_openai_params(
        model="omnivoice",
        optional_params={},
        voice=None,
        drop_params=False,
        kwargs={
            "script": [{"speaker": "S1", "text": "Hello"}],
            "speakers": [{"speaker": "S1", "voice": "clone:test"}],
        },
    )
    assert mapped["script"] == [{"speaker": "S1", "text": "Hello"}]
    assert mapped["speakers"] == [{"speaker": "S1", "voice": "clone:test"}]


def test_script_request_builds_json_body():
    voice, mapped = _config().map_openai_params(
        model="omnivoice",
        optional_params={},
        voice=None,
        drop_params=False,
        kwargs={
            "script": [{"speaker": "S1", "text": "Hello"}],
            "speakers": [{"speaker": "S1", "voice": "clone:test"}],
        },
    )
    data = _config().transform_text_to_speech_request(
        model="omnivoice",
        input="",
        voice=None,
        optional_params=mapped,
        litellm_params={},
        headers={},
    )
    assert "dict_body" in data
    assert "form_data" not in data

    body = data["dict_body"]
    assert body["model"] == "omnivoice"
    assert body["script"] == [{"speaker": "S1", "text": "Hello"}]
    assert body["speakers"] == [{"speaker": "S1", "voice": "clone:test"}]


def test_script_request_includes_optional_params():
    voice, mapped = _config().map_openai_params(
        model="omnivoice",
        optional_params={"speed": 1.5, "language": "ar"},
        voice=None,
        drop_params=False,
        kwargs={
            "script": [{"speaker": "S1", "text": "Hi"}],
            "speakers": [{"speaker": "S1", "voice": "clone:test"}],
        },
    )
    data = _config().transform_text_to_speech_request(
        model="omnivoice",
        input="",
        voice=None,
        optional_params=mapped,
        litellm_params={},
        headers={},
    )
    body = data["dict_body"]
    assert body["speed"] == 1.5
    assert body["language"] == "ar"


def test_script_response_returns_binary_content():
    response = httpx.Response(
        status_code=200,
        content=b"\x52\x49\x46\x46\x00\x00\x00\x00",
        headers={"content-type": "audio/wav"},
    )
    result = _config().transform_text_to_speech_response(
        model="omnivoice",
        raw_response=response,
        logging_obj=None,
    )
    assert result.response is response
