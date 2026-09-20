"""Regression tests for the OICM-custom provider config dispatch slice.

The dispatch for OICM-custom providers (Hamsa, OmniVoice, and the Inception
text-to-speech/transcription branches) was previously grafted directly into
``litellm.utils.ProviderConfigManager`` and got dropped in upstream merges. It
now lives in the co-located ``litellm.llms.oicm_providers`` registry, which
``ProviderConfigManager`` delegates to. These tests assert that the public
``ProviderConfigManager`` entry points still resolve the correct config classes,
so a merge that deletes the registry (or drops the delegation) fails loudly.
"""

import pytest

import litellm
from litellm.utils import ProviderConfigManager


@pytest.mark.parametrize(
    "provider_name,expected_class",
    [
        ("hamsa", "HamsaVoiceConfig"),
        ("omnivoice", "OmniVoiceVoiceConfig"),
    ],
)
def test_provider_voice_config_resolves_oicm_providers(
    provider_name: str, expected_class: str
) -> None:
    provider = litellm.LlmProviders(provider_name)
    config = ProviderConfigManager.get_provider_voice_config(provider)
    assert config is not None
    assert type(config).__name__ == expected_class


def test_provider_voice_config_returns_none_for_non_oicm() -> None:
    config = ProviderConfigManager.get_provider_voice_config(litellm.LlmProviders.OPENAI)
    assert config is None


def test_provider_script_config_resolves_omnivoice() -> None:
    config = ProviderConfigManager.get_provider_script_config(litellm.LlmProviders.OMNIVOICE)
    assert config is not None
    assert type(config).__name__ == "OmniVoiceScriptConfig"


@pytest.mark.parametrize(
    "provider_name,kwargs,expected_class",
    [
        ("hamsa", None, "HamsaTextToSpeechConfig"),
        ("inception", None, "InceptionTextToSpeechConfig"),
        ("omnivoice", None, "OmniVoiceTextToSpeechConfig"),
        ("omnivoice", {"ref_audio": b"x"}, "OmniVoiceVoiceCloneConfig"),
    ],
)
def test_provider_text_to_speech_config_resolves_oicm_providers(
    provider_name: str, kwargs, expected_class: str
) -> None:
    provider = litellm.LlmProviders(provider_name)
    config = ProviderConfigManager.get_provider_text_to_speech_config(
        model="x", provider=provider, kwargs=kwargs
    )
    assert config is not None
    assert type(config).__name__ == expected_class


def test_provider_text_to_speech_config_still_resolves_upstream() -> None:
    config = ProviderConfigManager.get_provider_text_to_speech_config(
        model="elevenlabs/mono", provider=litellm.LlmProviders.ELEVENLABS
    )
    assert config is not None
    assert type(config).__name__ == "ElevenLabsTextToSpeechConfig"


@pytest.mark.parametrize(
    "provider_name,expected_class",
    [
        ("hamsa", "HamsaAudioTranscriptionConfig"),
        ("inception", "InceptionAudioTranscriptionConfig"),
    ],
)
def test_provider_audio_transcription_config_resolves_oicm_providers(
    provider_name: str, expected_class: str
) -> None:
    provider = litellm.LlmProviders(provider_name)
    config = ProviderConfigManager.get_provider_audio_transcription_config(
        model="x", provider=provider
    )
    assert config is not None
    assert type(config).__name__ == expected_class


def test_oicm_registry_module_importable() -> None:
    import litellm.llms.oicm_providers as oicm

    assert callable(oicm.get_provider_voice_config)
    assert callable(oicm.get_provider_script_config)
    assert callable(oicm.get_provider_text_to_speech_config)
    assert callable(oicm.get_provider_audio_transcription_config)