from dataclasses import dataclass
from typing import Final, FrozenSet, List, Optional, Tuple

from .config import CLUSTER_DOMAIN, MODEL_PORT

# Separator for the composite model key `{uuid}::{model_id}`. A single
# deployment (uuid) can host multiple models behind the same ClusterIP, so the
# controller keys its state by this composite rather than by uuid alone.
COMPOSITE_KEY_SEP = "::"

# Providers recognized by substring in the deployment's owned_by / model id.
# Substring (not exact) matching, so suffixed ids like "hamsa-tts-new" still
# resolve to the "hamsa" provider.
KNOWN_PROVIDERS: Final[Tuple[str, ...]] = ("inception", "hamsa", "omnivoice")

# Providers whose pods serve a native REST surface instead of an OpenAI /v1
# API. Their LiteLLM config classes append their own paths to api_base (e.g.
# HamsaTextToSpeechConfig -> "<api_base>/tts/stream"), so the registered
# api_base must be the bare ClusterIP with no "/v1" suffix.
NATIVE_BASE_PROVIDERS: Final[FrozenSet[str]] = frozenset({"hamsa"})

# Hamsa API surface variants, mirrored in litellm.llms.hamsa.common_utils.
# The controller sniffs the pod's openapi.json and stamps the matching surface
# into litellm_params["api_surface"] so the gateway builds correct URLs.
API_SURFACE_NATIVE: str = "native"
API_SURFACE_V1: str = "v1"

# Path sets that identify each hamsa surface in an openapi.json probe.
_HAMSA_V1_PATHS: Final[FrozenSet[str]] = frozenset({"/v1/speech", "/v1/voices", "/v1/voice-clone"})
_HAMSA_NATIVE_PATHS: Final[FrozenSet[str]] = frozenset({"/tts/stream", "/transcribe"})


def detect_api_surface(provider: str, paths: FrozenSet[str]) -> Optional[str]:
    """Return the hamsa API surface implied by the pod's exposed paths, if any."""
    if provider != "hamsa":
        return None
    if _HAMSA_V1_PATHS & paths:
        return API_SURFACE_V1
    if _HAMSA_NATIVE_PATHS & paths:
        return API_SURFACE_NATIVE
    return None


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
    api_surface: Optional[str] = None

    @property
    def composite_key(self) -> str:
        return f"{self.uuid}{COMPOSITE_KEY_SEP}{self.model_name}"

    @property
    def api_base(self) -> str:
        if self.api_base_override:
            return self.api_base_override
        base = (
            f"http://s-{self.uuid}.{self.namespace}.{CLUSTER_DOMAIN}"
            f":{MODEL_PORT}"
        )
        if self.provider in NATIVE_BASE_PROVIDERS:
            return base
        return f"{base}/v1"

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
OCR_PATH = "/v1/ocr"
RERANK_PATHS: FrozenSet[str] = frozenset({"/v1/rerank", "/v2/rerank"})

# Native (non-OpenAI) REST paths exposed by Hamsa pods.
HAMSA_TTS_PATH = "/tts/stream"
HAMSA_TRANSCRIPTION_PATH = "/transcribe"


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

    if HAMSA_TTS_PATH in paths and CHAT_PATH not in paths:
        return "text_to_speech"

    if HAMSA_TRANSCRIPTION_PATH in paths and CHAT_PATH not in paths:
        return "audio_transcription"

    if OCR_PATH in paths and CHAT_PATH not in paths:
        return "ocr"

    if "whisper" in mid_lower or "asr" in mid_lower:
        return "audio_transcription"

    # Name fallback for pods whose openapi.json probe fails: hyphen-delimited
    # capability tokens ("hamsa-tts-new", "hamsa-stt-v2") rather than bare
    # substrings, so ids like "settings" don't trip the "tts" check.
    if "-tts" in mid_lower or mid_lower.startswith("tts"):
        return "text_to_speech"

    if "-stt" in mid_lower or mid_lower.startswith("stt"):
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
    """Infer the LiteLLM provider for a discovered model.

    Precedence: known-provider substring in owner/model id (matches suffixed
    ids like "hamsa-tts-new", not just a bare "hamsa") -> k2-fsa (omnivoice)
    -> /v1/ocr-only surface -> hosted_vllm.

    TODO(future): switch to provider-based matching on the model id itself when
    model ids are namespaced by provider (e.g. a literal `paddlex/abcd` model
    name). Today the OCR branch infers "paddlex" purely from the exposed
    `/v1/ocr` path, which would also mislabel a non-PaddleX /v1/ocr model.
    """
    owner_lower = owned_by.lower()
    mid_lower = model_id.lower()

    for p in KNOWN_PROVIDERS:
        if p in owner_lower or p in mid_lower:
            return p

    if "k2-fsa" in owner_lower or "k2fsa" in mid_lower:
        return "omnivoice"

    if OCR_PATH in paths and CHAT_PATH not in paths:
        return "paddlex"

    return "hosted_vllm"
