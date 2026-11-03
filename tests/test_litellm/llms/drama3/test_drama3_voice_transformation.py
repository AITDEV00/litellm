import pytest

from litellm.llms.drama3.voice.transformation import Drama3VoiceConfig


def _config() -> Drama3VoiceConfig:
    return Drama3VoiceConfig()


def test_drama3_voice_supported_actions():
    actions = Drama3VoiceConfig.SUPPORTED_VOICE_ACTIONS
    for a in ("register", "list", "delete_profile"):
        assert a in actions


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
