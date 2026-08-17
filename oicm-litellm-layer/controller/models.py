from dataclasses import dataclass
from typing import FrozenSet, List, Optional, Sequence

from .config import CLUSTER_DOMAIN, MODEL_PORT

# Separator for the composite model key `{uuid}::{model_id}`. A single
# deployment (uuid) can host multiple models behind the same ClusterIP, so the
# controller keys its state by this composite rather than by uuid alone.
COMPOSITE_KEY_SEP = "::"


@dataclass
class OicmModel:
    uuid: str
    model_id: str
    model_name: str
    namespace: str
    ready_replicas: int
    total_replicas: int
    mode: str = "chat"
    provider: str = "hosted_vllm"
    extra_args: str = ""
    source: str = "local"
    api_base_override: Optional[str] = None

    @property
    def composite_key(self) -> str:
        return f"{self.uuid}{COMPOSITE_KEY_SEP}{self.model_name}"

    @property
    def api_base(self) -> str:
        if self.api_base_override:
            return self.api_base_override
        return (
            f"http://s-{self.uuid}.{self.namespace}.{CLUSTER_DOMAIN}"
            f":{MODEL_PORT}/v1"
        )

    @property
    def is_ready(self) -> bool:
        return self.ready_replicas > 0


def parse_model_list(resp: dict) -> List[str]:
    """Normalize a `/v1/models` response into a flat list of model ids.

    Tolerates the three shapes seen in the wild:
    - OpenAI:  ``{"object": "list", "data": [{"id": ...}]}``
    - Triton:  ``[{...}...]`` a bare array with ``name`` per entry
    - Triton-style wrapper: ``{"models": [{"name": ...}]}`` (PaddleX Docling)
    """
    if not isinstance(resp, dict):
        return []

    data = resp.get("data")
    if isinstance(data, list):
        ids = [m.get("id") for m in data if isinstance(m, dict)]
        return [i for i in ids if isinstance(i, str) and i.strip()]

    models = resp.get("models")
    if isinstance(models, list):
        names = [m.get("name") for m in models if isinstance(m, dict)]
        return [n for n in names if isinstance(n, str) and n.strip()]

    return []


def sanitize_model_id(raw_id: str) -> str:
    raw_id = raw_id.strip()
    if raw_id.startswith("/"):
        return raw_id.lstrip("/").replace("/", "--")
    return raw_id


CHAT_PATH = "/v1/chat/completions"
TRANSCRIPTION_PATH = "/v1/audio/transcriptions"
TTS_PATH = "/v1/audio/speech"
EMBEDDING_PATH = "/v1/embeddings"
RERANK_PATHS: FrozenSet[str] = frozenset({"/v1/rerank", "/v2/rerank"})
# Any route under /v1/convert/* (file, source, async, batch) marks a Docling
# document-conversion deployment.
DOCUMENT_CONVERSION_PREFIX = "/v1/convert"
DOCLING_PROVIDER = "docling"


def _has_convert_path(paths: Sequence[str]) -> bool:
    return any(p == DOCUMENT_CONVERSION_PREFIX or p.startswith(f"{DOCUMENT_CONVERSION_PREFIX}/") for p in paths)


def detect_mode_from_paths(paths: FrozenSet[str], model_id: str, extra_args: str) -> str:
    mid_lower = model_id.lower()
    extra_lower = extra_args.lower()

    if "--runner pooling" in extra_lower and CHAT_PATH not in paths:
        return "embedding"

    if EMBEDDING_PATH in paths and CHAT_PATH not in paths:
        return "embedding"

    if RERANK_PATHS & paths and CHAT_PATH not in paths:
        return "rerank"

    if TRANSCRIPTION_PATH in paths and CHAT_PATH not in paths:
        return "audio_transcription"

    if TTS_PATH in paths and CHAT_PATH not in paths:
        return "text_to_speech"

    if _has_convert_path(paths):
        return "document_conversion"

    if "whisper" in mid_lower or "asr" in mid_lower:
        return "audio_transcription"

    return "chat"


def detect_mode(model_id: str, extra_args: str) -> str:
    return detect_mode_from_paths(frozenset(), model_id, extra_args)


# LiteLLM uses "audio_speech" as the mode value; the controller's internal mode
# is "text_to_speech". Centralize the translation so it can't drift in two places.
def to_litellm_mode(mode: str) -> str:
    if mode == "text_to_speech":
        return "audio_speech"
    return mode


def detect_provider(owned_by: str, model_id: str, paths: FrozenSet[str] = frozenset()) -> str:
    owner_lower = owned_by.lower()
    mid_lower = model_id.lower()

    known_providers = ("inception", "hamsa", "omnivoice")
    for p in known_providers:
        if p in owner_lower or p in mid_lower:
            return p

    if "k2-fsa" in owner_lower or "k2fsa" in mid_lower:
        return "omnivoice"

    if _has_convert_path(paths):
        return DOCLING_PROVIDER

    return "hosted_vllm"
