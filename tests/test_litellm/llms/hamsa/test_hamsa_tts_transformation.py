from unittest import mock

import httpx

from litellm.llms.hamsa.text_to_speech.transformation import HamsaTextToSpeechConfig


def test_hamsa_tts_supported_params():
    params = HamsaTextToSpeechConfig().get_supported_openai_params("hamsa-tts")
    for p in ("voice", "response_format", "speed"):
        assert p in params, f"{p} should be a supported TTS param"


def test_hamsa_tts_url():
    config = HamsaTextToSpeechConfig()
    url = config.get_complete_url(
        model="hamsa-tts",
        api_base="http://10.0.0.5:8080",
        litellm_params={},
    )
    assert url == "http://10.0.0.5:8080/tts/stream"


def test_hamsa_tts_url_strips_trailing_slash():
    config = HamsaTextToSpeechConfig()
    url = config.get_complete_url(
        model="hamsa-tts",
        api_base="http://10.0.0.5:8080/",
        litellm_params={},
    )
    assert url == "http://10.0.0.5:8080/tts/stream"


def test_hamsa_tts_url_requires_api_base():
    config = HamsaTextToSpeechConfig()
    try:
        config.get_complete_url(model="hamsa-tts", api_base=None, litellm_params={})
        assert False, "Should have raised for missing api_base"
    except Exception as e:
        assert "Missing Hamsa API base" in str(e)


def test_hamsa_tts_voice_required():
    config = HamsaTextToSpeechConfig()
    try:
        config.map_openai_params(
            model="hamsa-tts",
            optional_params={},
            voice=None,
            drop_params=False,
            kwargs={},
        )
        assert False, "Should have raised for missing voice"
    except Exception as e:
        assert "speaker" in str(e).lower()


def test_hamsa_tts_request_body():
    config = HamsaTextToSpeechConfig()
    speaker, mapped = config.map_openai_params(
        model="hamsa-tts",
        optional_params={
            "response_format": "mulaw",
            "speed": 1.0,
        },
        voice="jasem",
        drop_params=False,
        kwargs={},
    )
    assert speaker == "jasem"
    assert mapped["mulaw"] is True
    assert mapped["speed"] == 1.0

    request_data = config.transform_text_to_speech_request(
        model="hamsa-tts",
        input="مرحبا",
        voice="jasem",
        optional_params=dict(mapped),
        litellm_params={},
        headers={},
    )
    body = request_data["dict_body"]
    assert body["text"] == "مرحبا"
    assert body["speaker"] == "jasem"
    assert body["language_id"] == "ar"
    assert body["stream"] is False
    assert body["mulaw"] is True
    assert body["speed"] == 1.0


def test_hamsa_tts_router_level_params_filtered():
    config = HamsaTextToSpeechConfig()
    speaker, mapped = config.map_openai_params(
        model="hamsa-tts",
        optional_params={},
        voice="jasem",
        drop_params=False,
        kwargs={
            "use_in_pass_through": True,
            "use_litellm_proxy": True,
            "use_xai_oauth": True,
            "use_chat_completions_api": True,
            "merge_reasoning_content_in_choices": True,
            "custom_param": "kept",
        },
    )
    assert "use_in_pass_through" not in mapped
    assert "use_litellm_proxy" not in mapped
    assert "use_xai_oauth" not in mapped
    assert "use_chat_completions_api" not in mapped
    assert "merge_reasoning_content_in_choices" not in mapped
    assert mapped["custom_param"] == "kept"

    request_data = config.transform_text_to_speech_request(
        model="hamsa-tts",
        input="hello",
        voice="jasem",
        optional_params=dict(mapped),
        litellm_params={},
        headers={},
    )
    body = request_data["dict_body"]
    assert "use_in_pass_through" not in body
    assert "use_litellm_proxy" not in body
    assert "use_xai_oauth" not in body
    assert "use_chat_completions_api" not in body
    assert "merge_reasoning_content_in_choices" not in body
    assert body["custom_param"] == "kept"


def test_hamsa_tts_response_returns_binary():
    config = HamsaTextToSpeechConfig()
    fake_response = httpx.Response(
        status_code=200,
        content=b"\x49\x44\x33\x03",
        headers={"content-type": "audio/mpeg"},
        request=httpx.Request("POST", "http://x/tts/stream"),
    )
    result = config.transform_text_to_speech_response(
        model="hamsa-tts",
        raw_response=fake_response,
        logging_obj=mock.Mock(),
    )
    assert result.response.headers["content-type"] == "audio/mpeg"
