"""OICM custom diarization SDK vertical slice.

Co-located under ``litellm/endpoints/diarization/`` mirroring the
``litellm/endpoints/voice/`` slice shape. Public API (``litellm.adiarize``,
``litellm.diarize``) is re-exported lazily via ``litellm.__init__.__getattr__``.
"""

from litellm.endpoints.diarization.main import adiarize, diarize

__all__ = ["adiarize", "diarize"]
