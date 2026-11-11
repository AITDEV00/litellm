from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, FrozenSet, List, Mapping, Optional, Tuple

from .capabilities import CapabilityFacts, capability_metadata
from .config import CLUSTER_DOMAIN, CLUSTER_NAME, MODEL_PORT, NAMESPACE
from .token_limits import token_budgets

# A Submariner import namespaces its uuid as `submariner:<cluster>:<uuid>` to
# keep it unique in the k8s EndpointSlice. The owning OICM reports the bare
# uuid, so the two only join once this prefix is stripped. The prefix is a
# transport detail, not part of the deployment's identity.
SUBMARINER_PREFIX: Final[str] = "submariner:"


def strip_source_prefix(uuid: str) -> str:
    """The bare deployment uuid behind a possibly source-prefixed one.

    ``submariner:abudhabi:<uuid>`` becomes ``<uuid>``; a bare uuid passes
    through unchanged. A value that is prefixed but carries no cluster (a
    malformed value) is returned without the prefix rather than mangled into a
    different uuid.
    """
    if not uuid.startswith(SUBMARINER_PREFIX):
        return uuid
    _, _, rest = uuid.partition(SUBMARINER_PREFIX)
    _, _, bare = rest.partition(":")
    return bare or rest

# Providers recognized by substring in the deployment's owned_by / model id.
# Substring (not exact) matching, so suffixed ids like "hamsa-tts-new" still
# resolve to the "hamsa" provider.
KNOWN_PROVIDERS: Final[Tuple[str, ...]] = (
    "inception",
    "hamsa",
    "omnivoice",
    "pyannote",
    "drama3",
)

# Providers whose pods serve a native REST surface instead of an OpenAI /v1
# API. Their LiteLLM config classes append their own paths to api_base (e.g.
# HamsaTextToSpeechConfig -> "<api_base>/tts/stream"), so the registered
# api_base must be the bare ClusterIP with no "/v1" suffix.
NATIVE_BASE_PROVIDERS: Final[FrozenSet[str]] = frozenset({"hamsa", "pyannote"})

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


@dataclass(frozen=True, slots=True)
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
    # The cluster this deployment physically runs in: the cluster the controller
    # runs in for a local deployment, the source cluster the EndpointSlice came
    # from for a Submariner import. It answers "Abu Dhabi or Al Ain" for a model
    # and is stored as `oicm_cluster`, because the uuid alone cannot say it once a
    # cross-cluster import shares a model name with a local deployment.
    cluster: str = CLUSTER_NAME
    api_base_override: Optional[str] = None
    api_surface: Optional[str] = None
    # Lifecycle: False while the deployment exists in OICM but is not serving,
    # which is a Stopped deployment (no k8s Deployment, no Service) or one with
    # no ready replicas yet. A non-serving deployment stays registered and
    # visible but is paused, because leaving it routable would send every
    # request at a Service that no longer exists.
    serving: bool = True
    # The window the backend advertised in /v1/models (``max_model_len``), or
    # None when the backend does not expose one. Drives the token-budget
    # metadata this controller stamps; see token_limits.py.
    max_model_len: Optional[int] = None
    # What the backend's own modality probe reported (or the synthetic probe
    # classified) about image/audio acceptance. None means unknown and stamps
    # nothing; see capabilities.py.
    capability_facts: Optional[CapabilityFacts] = None
    # OICM's declared ModelServerTasks value for this deployment, stamped as
    # model_info.oicm_task so consumers see the operator's intent even when
    # the mounted surface (mode) serves it differently (a VibeVoice-ASR is
    # task=ASR, mode=chat).
    inference_task: Optional[str] = None

    @property
    def deployment_id(self) -> str:
        """The identity the reconciler keys this model by.

        A model is one deployment, so the uuid alone is the identity. Keying on
        uuid is what lets a deployment whose OICM GUI label differs from the id
        its server actually serves still match its gateway row: the served id is
        what the controller registered, so the stored and discovered names agree
        even when OICM's own ``model_name`` does not.

        The source prefix is stripped so a cross-cluster import and its own
        OICM's snapshot land on one key: the import knows it as
        ``submariner:<cluster>:<uuid>`` while OICM reports the bare uuid. The
        cluster is not lost by doing this, it moves to ``cluster``, which is
        persisted as ``oicm_cluster``.
        """
        return strip_source_prefix(self.uuid)
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
    def token_budgets(self) -> Mapping[str, int]:
        """The model_info token keys to stamp for this deployment."""
        return token_budgets(self.max_model_len, self.mode)

    @property
    def capability_stamp(self) -> Mapping[str, object]:
        """The model_info capability keys to stamp for this deployment."""
        return capability_metadata(self.mode, self.capability_facts)

    @property
    def health_check_stamp(self) -> Mapping[str, str]:
        """The model_info probe keys this deployment's health check needs.

        The gateway's audio_speech probe defaults voice="alloy" (an OpenAI
        name), which a task-declared TTS pod on this platform rejects with a
        400 (Drama 3 accepts only "default", "" or a registered voice),
        flipping the row health-red forever. Scoped to the hosted_vllm
        family: other TTS providers have their own voice contracts (hamsa
        requires a real speaker name, inception and omnivoice accept
        "alloy"), and a provider-scoped row that later gains a TTS task
        declaration must not silently inherit a voice its pod rejects.
        """
        if (
            self.mode == "text_to_speech"
            and self.inference_task == "Text-to-Speech"
            and self.provider not in NATIVE_BASE_PROVIDERS
        ):
            return {"health_check_voice": "default"}
        return {}


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
IMAGE_GENERATION_PATH = "/v1/images/generations"
RERANK_PATHS: FrozenSet[str] = frozenset({"/v1/rerank", "/v2/rerank"})

# Native (non-OpenAI) REST paths exposed by Hamsa pods.
HAMSA_TTS_PATH = "/tts/stream"
HAMSA_TRANSCRIPTION_PATH = "/transcribe"

# pyannoteAI-compatible diarization path served by coro deployments.
DIARIZE_PATH = "/v1/diarize"

# Ordered path to mode, most specific first. A path only implies a mode when the
# chat path is absent, so this is consulted once that is established. A frozenset
# value means any one of its members implies the mode.
_PATH_MODES: Final[Tuple[Tuple[object, str], ...]] = (
    (EMBEDDING_PATH, "embedding"),
    (RERANK_PATHS, "rerank"),
    # Diarize before transcription: a coro pod serves both surfaces, and the
    # table returns its first hit, so diarize-first classifies coro as a
    # diarization model. Pods serving transcriptions without diarize are
    # unaffected, and chat-capable servers never reach the table.
    (DIARIZE_PATH, "diarization"),
    (TRANSCRIPTION_PATH, "audio_transcription"),
    (TTS_PATH, "text_to_speech"),
    (HAMSA_TTS_PATH, "text_to_speech"),
    (HAMSA_TRANSCRIPTION_PATH, "audio_transcription"),
    (OCR_PATH, "ocr"),
)

# Name fallback for pods whose openapi.json probe fails. Hyphen-delimited
# capability tokens, so an id like "settings" does not trip the "tts" check.
_NAME_MODES: Final[Tuple[Tuple[str, str], ...]] = (
    ("whisper", "audio_transcription"),
    ("asr", "audio_transcription"),
    ("-tts", "text_to_speech"),
    ("-stt", "audio_transcription"),
    ("diariz", "diarization"),
)

# OICM's declared `inference_task` (ModelServerTasks, the operator's deploy-time
# intent) to controller mode. Data, not code, so a new task is one table row.
# Tasks absent from the table degrade to path detection rather than guessing.
# The mode values are the controller's internal vocabulary; to_litellm_mode
# translates at the boundary.
_TASK_MODES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Text Generation": "chat",
        "Image-Text-to-Text": "chat",
        "Any-to-Any": "chat",
        "Text-to-Speech": "text_to_speech",
        "Automatic Speech Recognition": "audio_transcription",
        "Text Embedding": "embedding",
        "Text-to-Image": "image_generation",
        "Diarization": "diarization",
    }
)

# The path(s) that prove a task's native surface is actually mounted. Tier 0
# only overrides when one of these is present; otherwise the mounted surface
# wins (a VibeVoice-ASR serves through chat and stays chat). Every non-chat
# task MUST have a gate entry: a task firing without its surface evidence is
# exactly the misregistration this tier exists to prevent.
_TASK_SURFACE_PATHS: Final[Mapping[str, Tuple[str, ...]]] = MappingProxyType(
    {
        "Text-to-Speech": (TTS_PATH,),
        "Automatic Speech Recognition": (TRANSCRIPTION_PATH,),
        "Text Embedding": (EMBEDDING_PATH,),
        "Text-to-Image": (IMAGE_GENERATION_PATH,),
        "Diarization": (DIARIZE_PATH,),
    }
)


def detect_mode_from_task(
    inference_task: Optional[str], paths: FrozenSet[str]
) -> Optional[str]:
    """The tier-0 mode decision from OICM's declared task, or None.

    The declaration is intent, the paths are the mounted surface, and the two
    can disagree: a VibeVoice-ASR declares `Automatic Speech Recognition` but
    serves through /v1/chat/completions with no transcription path mounted.
    Registering it as audio_transcription would make the gateway POST
    /v1/audio/transcriptions, which the pod does not serve. So the task's mode
    is used only when the task's native surface is actually mounted; otherwise
    the caller falls through to path detection, which sees the chat surface.

    Returns None for an unknown task, an absent task, or an unmounted surface,
    which keeps this tier strictly additive over detect_mode_from_paths.
    """
    if not inference_task:
        return None
    mode = _TASK_MODES.get(inference_task)
    if mode is None:
        return None
    surface_paths = _TASK_SURFACE_PATHS.get(inference_task)
    if surface_paths is not None and not any(p in paths for p in surface_paths):
        return None
    return mode


def detect_mode_from_paths(paths: FrozenSet[str], model_id: str, extra_args: str) -> str:
    """Infer the LiteLLM mode from the pod's exposed paths, then its name.

    A server that can chat is a chat model, whatever else it also serves. The
    reverse does not hold: a pod exposing only ``/v1/audio/transcriptions`` is an
    audio model. So the chat path decides first, and the table below is consulted
    only once chat is ruled out. Without that ordering, a chat server that also
    exposes a transcription endpoint gets registered as an audio model, and every
    request then 404s against an endpoint the pod does not serve.

    The name fallback covers pods whose ``openapi.json`` probe fails: it matches
    hyphen-delimited capability tokens (``hamsa-tts-new``, ``hamsa-stt-v2``)
    rather than bare substrings, so an id like ``settings`` does not trip the
    ``tts`` check.
    """
    if "--runner pooling" in extra_args.lower() and CHAT_PATH not in paths:
        return "embedding"

    if CHAT_PATH in paths:
        return "chat"

    for candidate, mode in _PATH_MODES:
        if isinstance(candidate, frozenset):
            if candidate & paths:
                return mode
        elif candidate in paths:
            return mode

    mid_lower = model_id.lower()
    for token, mode in _NAME_MODES:
        if token in mid_lower or mid_lower.startswith(token.lstrip("-")):
            return mode

    return "chat"


def detect_mode(model_id: str, extra_args: str) -> str:
    return detect_mode_from_paths(frozenset(), model_id, extra_args)


def build_model(
    *,
    uuid: str,
    model_id: str,
    ready_replicas: int,
    total_replicas: int,
    mode: str = "chat",
    provider: str = "hosted_vllm",
    source: str = "local",
    cluster: str = CLUSTER_NAME,
    extra_args: str = "",
    api_base_override: Optional[str] = None,
    api_surface: Optional[str] = None,
    namespace: str = NAMESPACE,
    max_model_len: Optional[int] = None,
    capability_facts: Optional[CapabilityFacts] = None,
    inference_task: Optional[str] = None,
) -> OicmModel:
    """Assemble an OicmModel, deriving model_name from the raw id.

    Single place that sanitizes the model id into model_name, so sources do not
    each repeat the sanitize call.
    """
    return OicmModel(
        uuid=uuid,
        model_id=model_id,
        model_name=sanitize_model_id(model_id),
        namespace=namespace,
        ready_replicas=ready_replicas,
        total_replicas=total_replicas,
        mode=mode,
        provider=provider,
        extra_args=extra_args,
        source=source,
        cluster=cluster,
        api_base_override=api_base_override,
        api_surface=api_surface,
        max_model_len=max_model_len,
        capability_facts=capability_facts,
        inference_task=inference_task,
    )


# LiteLLM uses "audio_speech" as the mode value; the controller's internal mode
# is "text_to_speech". Centralize the translation so it can't drift in two places.
def to_litellm_mode(mode: str) -> str:
    if mode == "text_to_speech":
        return "audio_speech"
    return mode


def build_oicm_model(
    *,
    uuid: str,
    model_id: str,
    model_name: str,
    namespace: str,
    serving: bool,
    source: str = "oicm",
    cluster: str = CLUSTER_NAME,
    provider: str = "hosted_vllm",
    api_base_override: Optional[str] = None,
    inference_task: Optional[str] = None,
) -> OicmModel:
    """Build the model record for a deployment known only from OICM.

    A Stopped deployment has no k8s Deployment and no Service, so it cannot be
    probed and its ClusterIP does not resolve. It still has to stay registered
    and visible, so this derives the same shape discovery would have produced
    and marks it non-serving, which is what pauses its routing.

    The provider defaults to hosted_vllm because the probe that would have
    distinguished a native surface cannot run. That is safe only because a
    non-serving deployment is never routed, and a deployment that starts again
    is re-discovered from k8s before it can serve.
    """
    return OicmModel(
        uuid=uuid,
        model_id=model_id,
        model_name=sanitize_model_id(model_name),
        namespace=namespace,
        ready_replicas=0,
        total_replicas=0,
        mode=to_litellm_mode("chat"),
        provider=provider,
        source=source,
        cluster=cluster,
        api_base_override=api_base_override,
        serving=serving,
        inference_task=inference_task,
    )


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

    # The drama-3 id carries a hyphen, so the KNOWN_PROVIDERS substring loop
    # cannot match it; both spellings are handled here. The owner string
    # (sglang-omni) is deliberately not matched: that serving stack also hosts
    # other models (cosyvoice, moss, fish-speech), which would mislabel.
    if "drama-3" in mid_lower or "drama3" in mid_lower:
        return "drama3"

    if DIARIZE_PATH in paths and CHAT_PATH not in paths:
        return "pyannote"

    if OCR_PATH in paths and CHAT_PATH not in paths:
        return "paddlex"

    if _HAMSA_NATIVE_PATHS & paths:
        return "hamsa"

    return "hosted_vllm"
