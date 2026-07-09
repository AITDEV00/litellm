from dataclasses import dataclass
from typing import Optional

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


def detect_mode(model_id: str, extra_args: str) -> str:
    mid_lower = model_id.lower()
    extra_lower = extra_args.lower()

    if "embedding" in mid_lower or "--runner pooling" in extra_lower:
        return "embedding"
    if "whisper" in mid_lower or "asr" in mid_lower:
        return "transcription"
    if "tts" in extra_lower or "text_to_speech" in extra_lower:
        return "tts_skip"
    return "chat"
