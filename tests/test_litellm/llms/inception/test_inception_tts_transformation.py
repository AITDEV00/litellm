import json
from unittest import mock

import httpx

from litellm.llms.inception.text_to_speech.transformation import (
    InceptionTextToSpeechConfig,
)


def test_inception_tts_supported_params():
    params = InceptionTextToSpeechConfig().get_supported_openai_params("inception-tts")
    for p in ("voice", "response_format", "speed", "language", "stream"):
        assert p in params, f"{p} should be a supported TTS param"


def test_inception_tts_url():
    config = InceptionTextToSpeechConfig()
    url = config.get_complete_url(
        model="inception-tts",
        api_base="http://10.0.0.5:8000",
        litellm_params={},
    )
    assert url == "http://10.0.0.5:8000/v1/audio/speech"


def test_inception_tts_url_strips_trailing_slash():
    config = InceptionTextToSpeechConfig()
    url = config.get_complete_url(
        model="inception-tts",
        api_base="http://10.0.0.5:8000/",
        litellm_params={},
    )
    assert url == "http://10.0.0.5:8000/v1/audio/speech"


def test_inception_tts_url_requires_api_base():
    config = InceptionTextToSpeechConfig()
    try:
        config.get_complete_url(model="inception-tts", api_base=None, litellm_params={})
        assert False, "Should have raised for missing api_base"
    except Exception as e:
        assert "Missing Inception API base" in str(e)


def test_inception_tts_request_body_includes_language_and_stream():
    config = InceptionTextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="inception-tts",
        optional_params={
            "response_format": "mp3",
            "speed": 1.0,
            "language": "ar",
            "stream": False,
        },
        voice="samia",
        drop_params=False,
        kwargs={},
    )
    assert voice == "samia"
    assert mapped["response_format"] == "mp3"
    assert mapped["speed"] == 1.0
    assert mapped["language"] == "ar"
    assert mapped["stream"] is False

    request_data = config.transform_text_to_speech_request(
        model="inception-tts",
        input="مرحبا",
        voice="samia",
        optional_params=dict(mapped),
        litellm_params={},
        headers={},
    )
    body = request_data["dict_body"]
    assert body["model"] == "inception-tts"
    assert body["input"] == "مرحبا"
    assert body["voice"] == "samia"
    assert body["response_format"] == "mp3"
    assert body["speed"] == 1.0
    assert body["language"] == "ar"
    assert body["stream"] is False


def test_inception_tts_default_voice_when_none():
    config = InceptionTextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="inception-tts",
        optional_params={},
        voice=None,
        drop_params=False,
        kwargs={},
    )
    assert voice is None

    request_data = config.transform_text_to_speech_request(
        model="inception-tts",
        input="hello",
        voice=None,
        optional_params={},
        litellm_params={},
        headers={},
    )
    body = request_data["dict_body"]
    assert body["voice"] == "alloy"


def test_inception_tts_validate_environment_sets_content_type():
    config = InceptionTextToSpeechConfig()
    headers = config.validate_environment(headers={}, model="inception-tts", api_key=None, api_base="http://x")
    assert headers["Content-Type"] == "application/json"


def test_inception_tts_response_returns_binary():
    config = InceptionTextToSpeechConfig()
    fake_response = httpx.Response(
        status_code=200,
        content=b"\x49\x44\x33\x03",
        headers={"content-type": "audio/mpeg"},
        request=httpx.Request("POST", "http://x/v1/audio/speech"),
    )
    result = config.transform_text_to_speech_response(
        model="inception-tts",
        raw_response=fake_response,
        logging_obj=mock.Mock(),
    )
    assert result.response.headers["content-type"] == "audio/mpeg"


def test_inception_tts_extra_body_forwarded():
    config = InceptionTextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="inception-tts",
        optional_params={
            "extra_body": {"custom_param": "value"},
        },
        voice="samia",
        drop_params=False,
        kwargs={},
    )
    assert mapped["custom_param"] == "value"


def test_inception_tts_internal_params_filtered():
    config = InceptionTextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="inception-tts",
        optional_params={},
        voice="samia",
        drop_params=False,
        kwargs={
            "api_key": "secret",
            "litellm_call_id": "abc",
            "proxy_server_request": {},
            "custom_param": "kept",
        },
    )
    assert "api_key" not in mapped
    assert "litellm_call_id" not in mapped
    assert "proxy_server_request" not in mapped
    assert mapped["custom_param"] == "kept"
