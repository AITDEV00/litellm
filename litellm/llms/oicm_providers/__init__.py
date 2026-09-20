"""OICM custom provider configuration dispatch (vertical slice).

Contains the configuration dispatch for OICM-custom providers (Hamsa, OmniVoice)
and the OICM-custom branches grafted into Inception. Delegated into from
``litellm.utils.ProviderConfigManager``.
"""

from litellm.llms.oicm_providers.registry import (
    get_provider_audio_transcription_config,
    get_provider_script_config,
    get_provider_text_to_speech_config,
    get_provider_voice_config,
)

__all__ = [
    "get_provider_audio_transcription_config",
    "get_provider_script_config",
    "get_provider_text_to_speech_config",
    "get_provider_voice_config",
]