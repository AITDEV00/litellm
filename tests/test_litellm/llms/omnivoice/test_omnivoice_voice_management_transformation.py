import httpx

from litellm.llms.omnivoice.voice.transformation import OmniVoiceVoiceConfig


def _config() -> OmniVoiceVoiceConfig:
    return OmniVoiceVoiceConfig()


_BASE = "http://localhost:18080"


def test_list_voices_url():
    url = _config().get_complete_url(
        model="omnivoice",
        api_base=_BASE,
        litellm_params={"voice_action": "list"},
    )
    assert url == _BASE + "/v1/voices"


def test_list_profiles_url():
    url = _config().get_complete_url(
        model="omnivoice",
        api_base=_BASE,
        litellm_params={"voice_action": "list_profiles"},
    )
    assert url == _BASE + "/v1/voices"


def test_get_profile_url():
    url = _config().get_complete_url(
        model="omnivoice",
        api_base=_BASE,
        litellm_params={"voice_action": "get_profile", "profile_id": "Morgan"},
    )
    assert url == _BASE + "/v1/voices/profiles/Morgan"


def test_create_profile_url():
    url = _config().get_complete_url(
        model="omnivoice",
        api_base=_BASE,
        litellm_params={"voice_action": "create_profile"},
    )
    assert url == _BASE + "/v1/voices/profiles"


def test_update_profile_url():
    url = _config().get_complete_url(
        model="omnivoice",
        api_base=_BASE,
        litellm_params={"voice_action": "update_profile", "profile_id": "Morgan"},
    )
    assert url == _BASE + "/v1/voices/profiles/Morgan"


def test_delete_profile_url():
    url = _config().get_complete_url(
        model="omnivoice",
        api_base=_BASE,
        litellm_params={"voice_action": "delete_profile", "profile_id": "Morgan"},
    )
    assert url == _BASE + "/v1/voices/profiles/Morgan"


def test_url_strips_trailing_v1():
    url = _config().get_complete_url(
        model="omnivoice",
        api_base=_BASE + "/v1",
        litellm_params={"voice_action": "list"},
    )
    assert url == _BASE + "/v1/voices"


def test_get_profile_requires_profile_id():
    try:
        _config().get_complete_url(
            model="omnivoice",
            api_base=_BASE,
            litellm_params={"voice_action": "get_profile"},
        )
        assert False, "Should have raised for missing profile_id"
    except Exception as e:
        assert "profile_id" in str(e)


def test_update_profile_requires_profile_id():
    try:
        _config().get_complete_url(
            model="omnivoice",
            api_base=_BASE,
            litellm_params={"voice_action": "update_profile"},
        )
        assert False, "Should have raised for missing profile_id"
    except Exception as e:
        assert "profile_id" in str(e)


def test_url_requires_api_base():
    try:
        _config().get_complete_url(
            model="omnivoice",
            api_base=None,
            litellm_params={"voice_action": "list"},
        )
        assert False, "Should have raised for missing api_base"
    except Exception as e:
        assert "Missing OmniVoice API base" in str(e)


def test_list_request_returns_get_method():
    data = _config().transform_create_voice_request(
        model="omnivoice",
        voice_data={},
        optional_params={},
        litellm_params={"voice_action": "list"},
        headers={},
    )
    assert data.get("method") == "GET"
    assert "dict_body" not in data
    assert "form_data" not in data


def test_list_profiles_request_returns_get_method():
    data = _config().transform_create_voice_request(
        model="omnivoice",
        voice_data={},
        optional_params={},
        litellm_params={"voice_action": "list_profiles"},
        headers={},
    )
    assert data.get("method") == "GET"


def test_get_profile_request_returns_get_method():
    data = _config().transform_create_voice_request(
        model="omnivoice",
        voice_data={},
        optional_params={},
        litellm_params={"voice_action": "get_profile", "profile_id": "Morgan"},
        headers={},
    )
    assert data.get("method") == "GET"


def test_delete_profile_request_returns_delete_method():
    data = _config().transform_create_voice_request(
        model="omnivoice",
        voice_data={},
        optional_params={},
        litellm_params={"voice_action": "delete_profile", "profile_id": "Morgan"},
        headers={},
    )
    assert data.get("method") == "DELETE"
    assert "dict_body" not in data
    assert "form_data" not in data


def test_create_profile_request_builds_multipart_form_data():
    ref_audio = ("ref.wav", b"\x52\x49\x46\x46", "audio/wav")
    data = _config().transform_create_voice_request(
        model="omnivoice",
        voice_data={
            "profile_id": "test_profile",
            "ref_text": "reference transcript",
            "ref_audio": ref_audio,
        },
        optional_params={},
        litellm_params={"voice_action": "create_profile"},
        headers={},
    )
    assert "form_data" in data
    assert "files" in data
    assert "dict_body" not in data

    form = data["form_data"]
    assert form["profile_id"] == "test_profile"
    assert form["ref_text"] == "reference transcript"

    files = data["files"]
    assert "ref_audio" in files
    assert files["ref_audio"] == ref_audio


def test_create_profile_request_requires_profile_id():
    try:
        _config().transform_create_voice_request(
            model="omnivoice",
            voice_data={"ref_audio": ("ref.wav", b"\x00", "audio/wav")},
            optional_params={},
            litellm_params={"voice_action": "create_profile"},
            headers={},
        )
        assert False, "Should have raised for missing profile_id"
    except Exception as e:
        assert "profile_id" in str(e)


def test_create_profile_request_without_ref_audio():
    data = _config().transform_create_voice_request(
        model="omnivoice",
        voice_data={"profile_id": "test_profile", "ref_text": "text"},
        optional_params={},
        litellm_params={"voice_action": "create_profile"},
        headers={},
    )
    assert "form_data" in data
    assert data["files"] == {}
    assert data["form_data"]["profile_id"] == "test_profile"


def test_update_profile_request_uses_form_data_and_patch_method():
    data = _config().transform_create_voice_request(
        model="omnivoice",
        voice_data={"ref_text": "updated text", "speaker": "Speaker1"},
        optional_params={},
        litellm_params={"voice_action": "update_profile", "profile_id": "Morgan"},
        headers={},
    )
    assert data.get("method") == "PATCH"
    assert "form_data" in data
    assert "dict_body" not in data

    form = data["form_data"]
    assert form["ref_text"] == "updated text"
    assert form["speaker"] == "Speaker1"


def test_update_profile_request_skips_none_fields():
    data = _config().transform_create_voice_request(
        model="omnivoice",
        voice_data={"ref_text": "updated text", "speaker": None, "name": None},
        optional_params={},
        litellm_params={"voice_action": "update_profile", "profile_id": "Morgan"},
        headers={},
    )
    form = data["form_data"]
    assert form == {"ref_text": "updated text"}


def test_update_profile_request_empty_voice_data():
    data = _config().transform_create_voice_request(
        model="omnivoice",
        voice_data={},
        optional_params={},
        litellm_params={"voice_action": "update_profile", "profile_id": "Morgan"},
        headers={},
    )
    assert data.get("method") == "PATCH"
    assert data["form_data"] == {}


def test_transform_response_with_json_body():
    response = httpx.Response(
        status_code=200,
        json={"profile_id": "Morgan", "name": "Morgan", "ref_text": "hello"},
    )
    result = _config().transform_create_voice_response(
        model="omnivoice",
        raw_response=response,
        logging_obj=None,
    )
    assert result["profile_id"] == "Morgan"
    assert result["ref_text"] == "hello"


def test_transform_response_with_empty_body_returns_success():
    response = httpx.Response(status_code=204, content=b"")
    result = _config().transform_create_voice_response(
        model="omnivoice",
        raw_response=response,
        logging_obj=None,
    )
    assert result["status"] == "success"
    assert result["status_code"] == 204
