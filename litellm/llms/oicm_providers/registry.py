"""OICM custom provider configuration dispatch.

Self-contained vertical slice. Co-located under ``litellm/llms/oicm_providers/``
mirroring how ``litellm/llms/<provider>/`` hosts each provider's transformation
classes.

Upstream never touches this module. It owns all configuration dispatch for the
OICM-custom providers (Hamsa, OmniVoice) and the OICM-custom branches grafted
into the upstream Inception provider (text-to-speech and transcription). The
public entry points are imported and delegated to by
``litellm.utils.ProviderConfigManager``; each of the four methods mirrors the
upstream dispatch signature so ``ProviderConfigManager`` can route to it with a
single clean line.
"""

from __future__ import annotations

from collections.abc import Mapping

import litellm
from litellm.types.utils import LlmProviders
from litellm.llms.base_llm.audio_transcription.transformation import (
    BaseAudioTranscriptionConfig,
)
from litellm.llms.base_llm.diarization.transformation import (
    BaseDiarizationConfig,
)
from litellm.llms.base_llm.text_to_speech.transformation import (
    BaseTextToSpeechConfig,
)
from litellm.llms.base_llm.voice.transformation import BaseVoiceConfig


def get_provider_voice_config(
    provider: LlmProviders,
) -> BaseVoiceConfig | None:
    """Voice-management config for OICM providers (Hamsa, OmniVoice)."""
    if provider == litellm.LlmProviders.HAMSA:
        from litellm.llms.hamsa.voice.transformation import HamsaVoiceConfig

        return HamsaVoiceConfig()
    if provider == litellm.LlmProviders.OMNIVOICE:
        from litellm.llms.omnivoice.voice.transformation import OmniVoiceVoiceConfig

        return OmniVoiceVoiceConfig()
    return None


def get_provider_script_config(
    provider: LlmProviders,
) -> BaseTextToSpeechConfig | None:
    """Script-synthesis config for an OICM-custom provider (OmniVoice)."""
    if provider == litellm.LlmProviders.OMNIVOICE:
        from litellm.llms.omnivoice.script.transformation import (
            OmniVoiceScriptConfig,
        )

        return OmniVoiceScriptConfig()
    return None


def get_provider_text_to_speech_config(
    provider: LlmProviders,
    kwargs: Mapping[str, object] | None = None,
) -> BaseTextToSpeechConfig | None:
    """Text-to-speech config for OICM-custom providers.

    For providers with multiple TTS modes (e.g. OmniVoice voice cloning),
    *kwargs* is inspected to select the correct config subclass.
    """
    if provider == litellm.LlmProviders.HAMSA:
        if kwargs is not None and kwargs.get("ref_audio") is not None:
            from litellm.llms.hamsa.voice.transformation import (
                HamsaVoiceCloneConfig,
            )

            return HamsaVoiceCloneConfig()
        from litellm.llms.hamsa.text_to_speech.transformation import (
            HamsaTextToSpeechConfig,
        )

        return HamsaTextToSpeechConfig()
    if provider == litellm.LlmProviders.INCEPTION:
        from litellm.llms.inception.text_to_speech.transformation import (
            InceptionTextToSpeechConfig,
        )

        return InceptionTextToSpeechConfig()
    if provider == litellm.LlmProviders.OMNIVOICE:
        if kwargs is not None and kwargs.get("ref_audio") is not None:
            from litellm.llms.omnivoice.voice.transformation import (
                OmniVoiceVoiceCloneConfig,
            )

            return OmniVoiceVoiceCloneConfig()
        from litellm.llms.omnivoice.text_to_speech.transformation import (
            OmniVoiceTextToSpeechConfig,
        )

        return OmniVoiceTextToSpeechConfig()
    return None


def get_provider_audio_transcription_config(
    provider: LlmProviders,
) -> BaseAudioTranscriptionConfig | None:
    """Audio-transcription config for OICM-custom providers (Hamsa, Inception)."""
    if provider == litellm.LlmProviders.HAMSA:
        from litellm.llms.hamsa.transcription.transformation import (
            HamsaAudioTranscriptionConfig,
        )

        return HamsaAudioTranscriptionConfig()
    if provider == litellm.LlmProviders.INCEPTION:
        from litellm.llms.inception.transcription.transformation import (
            InceptionAudioTranscriptionConfig,
        )

        return InceptionAudioTranscriptionConfig()
    return None


def get_provider_diarization_config(
    provider: LlmProviders,
) -> BaseDiarizationConfig | None:
    """Diarization config for OICM-custom providers (pyannote)."""
    if provider == litellm.LlmProviders.PYANNOTE:
        from litellm.llms.pyannote.diarization.transformation import (
            PyannoteDiarizationConfig,
        )

        return PyannoteDiarizationConfig()
    return None