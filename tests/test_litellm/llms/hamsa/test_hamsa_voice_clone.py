"""Tests for HamsaVoiceCloneConfig (the /v1/audio/speech/clone route config)."""

import pytest

from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.hamsa.voice.transformation import HamsaVoiceCloneConfig


def test_v1_url_uses_v1_voice_clone_path():
    config = HamsaVoiceCloneConfig()
    url = config.get_complete_url(
        model="hamsa-tts-new",
        api_base="http://10.0.0.5:8080",
        litellm_params={"api_surface": "v1"},
    )
    assert url == "http://10.0.0.5:8080/v1/voice-clone"


def test_native_url_uses_tts_voice_clone_path():
    config = HamsaVoiceCloneConfig()
    url = config.get_complete_url(
        model="hamsa-tts",
        api_base="http://10.0.0.5:8080",
        litellm_params={},
    )
    assert url == "http://10.0.0.5:8080/tts/voice_clone"


def test_v1_rejects_uploaded_ref_audio_with_clear_400():
    """v1 pods accept only audio_url JSON; uploaded bytes cannot be forwarded.

    Previously the bytes tuple leaked into the JSON body and failed with an
    opaque 'Object of type bytes is not JSON serializable' 500.
    """
    config = HamsaVoiceCloneConfig()
    with pytest.raises(BaseLLMException, match="audio_url"):
        config.transform_text_to_speech_request(
            model="hamsa-tts-new",
            input="hello",
            voice="clone",
            optional_params={"ref_audio": ("ref.wav", b"RIFF", "audio/wav")},
            litellm_params={"api_surface": "v1"},
            headers={},
        )


def test_v1_with_audio_url_builds_json_body():
    config = HamsaVoiceCloneConfig()
    request_data = config.transform_text_to_speech_request(
        model="hamsa-tts-new",
        input="transcript of the clip",
        voice="clone",
        optional_params={},
        litellm_params={"api_surface": "v1", "audio_url": "http://10.0.0.9/ref.wav"},
        headers={},
    )
    assert request_data["dict_body"] == {
        "audio_url": "http://10.0.0.9/ref.wav",
        "prompt_text": "transcript of the clip",
    }


def test_v1_without_any_url_is_a_400():
    config = HamsaVoiceCloneConfig()
    with pytest.raises(BaseLLMException, match="audio_url"):
        config.transform_text_to_speech_request(
            model="hamsa-tts-new",
            input="hello",
            voice="clone",
            optional_params={},
            litellm_params={"api_surface": "v1"},
            headers={},
        )


def test_native_forwards_multipart_upload():
    config = HamsaVoiceCloneConfig()
    request_data = config.transform_text_to_speech_request(
        model="hamsa-tts",
        input="hello",
        voice="clone",
        optional_params={"ref_audio": ("ref.wav", b"RIFF0000", "audio/wav")},
        litellm_params={},
        headers={},
    )
    assert "ref_audio" in request_data["files"]
    assert request_data["form_data"]["text"] == "hello"


def test_keyless_v1_config_does_not_raise():
    config = HamsaVoiceCloneConfig()
    headers = config.validate_environment(
        headers={}, model="hamsa-tts-new", api_key="", api_base=None
    )
    assert "x-api-key" not in headers


def test_registry_selects_clone_config_when_ref_audio_present():
    import litellm
    from litellm.utils import ProviderConfigManager

    config = ProviderConfigManager.get_provider_text_to_speech_config(
        model="hamsa-tts-new",
        provider=litellm.LlmProviders.HAMSA,
        kwargs={"ref_audio": ("ref.wav", b"RIFF", "audio/wav")},
    )
    assert type(config).__name__ == "HamsaVoiceCloneConfig"


def test_registry_selects_plain_config_without_ref_audio():
    import litellm
    from litellm.utils import ProviderConfigManager

    config = ProviderConfigManager.get_provider_text_to_speech_config(
        model="hamsa-tts-new",
        provider=litellm.LlmProviders.HAMSA,
        kwargs={},
    )
    assert type(config).__name__ == "HamsaTextToSpeechConfig"
