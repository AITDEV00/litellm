from dataclasses import dataclass
from typing import FrozenSet, Optional

from .config import CLUSTER_DOMAIN, MODEL_PORT


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
    litellm_model_id: Optional[str] = None
    extra_args: str = ""
    source: str = "local"
    api_base_override: Optional[str] = None

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


def detect_mode_from_paths(paths: FrozenSet[str], model_id: str, extra_args: str) -> str:  # noqa: PLR0911
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

    if "whisper" in mid_lower or "asr" in mid_lower:
        return "audio_transcription"

    return "chat"


def detect_mode(model_id: str, extra_args: str) -> str:
    return detect_mode_from_paths(frozenset(), model_id, extra_args)


def detect_provider(owned_by: str, model_id: str) -> str:
    owner_lower = owned_by.lower()
    mid_lower = model_id.lower()

    known_providers = ("inception", "hamsa", "omnivoice")
    for p in known_providers:
        if p in owner_lower or p in mid_lower:
            return p

    if "k2-fsa" in owner_lower or "k2fsa" in mid_lower:
        return "omnivoice"

    return "hosted_vllm"
