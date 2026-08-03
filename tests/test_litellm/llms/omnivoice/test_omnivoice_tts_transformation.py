import httpx
from unittest import mock

import litellm
from litellm.llms.omnivoice.text_to_speech.transformation import (
    OmniVoiceTextToSpeechConfig,
)


def test_omnivoice_in_provider_list():
    assert "omnivoice" in litellm.provider_list
    assert "omnivoice" in litellm.openai_compatible_providers


def test_omnivoice_in_custom_audio_handler_providers():
    from litellm.main import _CUSTOM_AUDIO_HANDLER_PROVIDERS

    assert "omnivoice" in _CUSTOM_AUDIO_HANDLER_PROVIDERS


def test_omnivoice_get_provider_text_to_speech_config():
    from litellm.utils import ProviderConfigManager
    from litellm.types.utils import LlmProviders

    config = ProviderConfigManager.get_provider_text_to_speech_config(
        model="omnivoice",
        provider=LlmProviders.OMNIVOICE,
    )
    assert isinstance(config, OmniVoiceTextToSpeechConfig)


def test_omnivoice_tts_supported_params():
    params = OmniVoiceTextToSpeechConfig().get_supported_openai_params("omnivoice")
    for p in ("voice", "response_format", "speed", "language", "stream"):
        assert p in params, f"{p} should be a supported TTS param"


def test_omnivoice_tts_url():
    config = OmniVoiceTextToSpeechConfig()
    url = config.get_complete_url(
        model="omnivoice",
        api_base="http://10.43.173.29:8080",
        litellm_params={},
    )
    assert url == "http://10.43.173.29:8080/v1/audio/speech"


def test_omnivoice_tts_url_strips_trailing_v1():
    config = OmniVoiceTextToSpeechConfig()
    url = config.get_complete_url(
        model="omnivoice",
        api_base="http://10.43.173.29:8080/v1",
        litellm_params={},
    )
    assert url == "http://10.43.173.29:8080/v1/audio/speech"


def test_omnivoice_tts_url_strips_trailing_slash():
    config = OmniVoiceTextToSpeechConfig()
    url = config.get_complete_url(
        model="omnivoice",
        api_base="http://10.43.173.29:8080/",
        litellm_params={},
    )
    assert url == "http://10.43.173.29:8080/v1/audio/speech"


def test_omnivoice_tts_url_requires_api_base():
    config = OmniVoiceTextToSpeechConfig()
    try:
        config.get_complete_url(model="omnivoice", api_base=None, litellm_params={})
        assert False, "Should have raised for missing api_base"
    except Exception as e:
        assert "Missing OmniVoice API base" in str(e)


def test_omnivoice_tts_request_body_includes_language_and_custom_params():
    config = OmniVoiceTextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={
            "response_format": "mp3",
            "speed": 1.0,
            "language": "ar",
            "stream": False,
            "num_step": 32,
            "guidance_scale": 3.0,
        },
        voice="alloy",
        drop_params=False,
        kwargs={},
    )
    assert voice == "alloy"
    assert mapped["response_format"] == "mp3"
    assert mapped["speed"] == 1.0
    assert mapped["language"] == "ar"
    assert mapped["stream"] is False
    assert mapped["num_step"] == 32
    assert mapped["guidance_scale"] == 3.0

    request_data = config.transform_text_to_speech_request(
        model="omnivoice",
        input="hello world",
        voice="alloy",
        optional_params=dict(mapped),
        litellm_params={},
        headers={},
    )
    body = request_data["dict_body"]
    assert body["model"] == "omnivoice"
    assert body["input"] == "hello world"
    assert body["voice"] == "alloy"
    assert body["response_format"] == "mp3"
    assert body["speed"] == 1.0
    assert body["language"] == "ar"
    assert body["stream"] is False
    assert body["num_step"] == 32
    assert body["guidance_scale"] == 3.0


def test_omnivoice_tts_default_voice_when_none():
    config = OmniVoiceTextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={},
        voice=None,
        drop_params=False,
        kwargs={},
    )
    assert voice is None

    request_data = config.transform_text_to_speech_request(
        model="omnivoice",
        input="hello",
        voice=None,
        optional_params={},
        litellm_params={},
        headers={},
    )
    body = request_data["dict_body"]
    assert body["voice"] == "auto"


def test_omnivoice_tts_instructions_filtered():
    config = OmniVoiceTextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={"instructions": "speak slowly"},
        voice="alloy",
        drop_params=False,
        kwargs={},
    )
    assert "instructions" not in mapped


def test_omnivoice_tts_internal_params_filtered():
    config = OmniVoiceTextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={},
        voice="alloy",
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


def test_omnivoice_tts_router_level_params_filtered():
    config = OmniVoiceTextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={},
        voice="alloy",
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


def test_omnivoice_tts_no_api_key_required():
    assert OmniVoiceTextToSpeechConfig.get_api_key() == "no-api-key-required"
    assert OmniVoiceTextToSpeechConfig.get_api_key("provided-key") == "provided-key"


def test_omnivoice_tts_validate_environment_does_not_force_content_type():
    config = OmniVoiceTextToSpeechConfig()
    headers = config.validate_environment(headers={}, model="omnivoice", api_key=None, api_base="http://x")
    assert "Content-Type" not in headers


def test_omnivoice_tts_response_returns_binary():
    config = OmniVoiceTextToSpeechConfig()
    fake_response = httpx.Response(
        status_code=200,
        content=b"\x49\x44\x33\x03",
        headers={"content-type": "audio/mpeg"},
        request=httpx.Request("POST", "http://x/v1/audio/speech"),
    )
    result = config.transform_text_to_speech_response(
        model="omnivoice",
        raw_response=fake_response,
        logging_obj=mock.Mock(),
    )
    assert result.response.headers["content-type"] == "audio/mpeg"


def test_omnivoice_tts_request_uses_dict_body_not_form_data():
    config = OmniVoiceTextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={"response_format": "mp3"},
        voice="alloy",
        drop_params=False,
        kwargs={},
    )
    request_data = config.transform_text_to_speech_request(
        model="omnivoice",
        input="hello",
        voice="alloy",
        optional_params=dict(mapped),
        litellm_params={},
        headers={},
    )
    assert "dict_body" in request_data
    assert "form_data" not in request_data


def test_omnivoice_tts_extra_body_forwarded():
    config = OmniVoiceTextToSpeechConfig()
    voice, mapped = config.map_openai_params(
        model="omnivoice",
        optional_params={
            "extra_body": {"num_step": 16, "guidance_scale": 2.0},
        },
        voice="alloy",
        drop_params=False,
        kwargs={},
    )
    assert mapped["num_step"] == 16
    assert mapped["guidance_scale"] == 2.0
