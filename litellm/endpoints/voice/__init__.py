"""OICM custom voice/script SDK vertical slice.

Co-located under ``litellm/endpoints/voice/`` mirroring the upstream
``litellm/endpoints/speech/speech_to_completion_bridge`` shape. Public API
(``litellm.acreate_voice``, ``litellm.create_voice``, ``litellm.ascript``,
``litellm.script``) is re-exported lazily via ``litellm.__init__.__getattr__``.
"""

from litellm.endpoints.voice.main import (
    acreate_voice,
    ascript,
    create_voice,
    script,
)

__all__ = ["acreate_voice", "ascript", "create_voice", "script"]
