"""Capability metadata the controller stamps so the gateway advertises what
each deployment actually accepts, instead of leaving every capability key
null for clients to guess.

The litellm vscode extension gates its wire format on these keys:
`mapModelInfoEntry` derives a deployment's input modalities from
``supports_vision`` and ``supports_audio_input`` being exactly ``true``, and
the send path drops an attached image (or audio part) when the modality is
absent, so an unadvertised capability is not merely under-reported, it
silently discards user content. The extension builds its own
``architecture`` block client-side from those flags, so the server stamps
the flags alone.

Facts come from the deployment itself, never a hand-maintained table:

- sglang serves ``/model_info`` with server-computed flags
  (``has_image_understanding``, ``has_audio_understanding``) derived from
  the loaded model config, so they cannot disagree with what the
  tokenizer/prefill path actually accepts.
- vLLM and other servers expose no such surface; for those the source sends
  one synthetic image and one synthetic audio chat request directly to the
  pod and classifies the verdict (200 -> accepts, an explicit 400 -> rejects,
  anything else -> unknown). The probe result is memoized per deployment for
  the controller's lifetime, so the cost is one request pair per restart.

A stored ``model_info`` key wins per-key over the gateway's discovery, and
the gateway's own refresh only fills token fields, so a stamped capability
stays stamped (verified live on dev across a refresh cycle).
"""

from dataclasses import dataclass
from typing import Final
from collections.abc import Mapping

# Servers whose /v1/models self-describes as a text-to-image (TI2I) surface
# report has_image_understanding=true for their DiT's image-edit path, but
# their "chat" endpoint generates images; stamping supports_vision would
# route understanding-style image chats to a diffusion server. A served
# task-type list that names TI2I without a generate/chat task suppresses the
# capability stamp entirely: claiming nothing is the honest answer.
_IMAGE_GEN_TASK_TYPES: Final[frozenset[str]] = frozenset({"TI2I", "T2I", "I2I", "image_generation"})


@dataclass(frozen=True, slots=True)
class CapabilityFacts:
    """The tri-state facts one deployment's probe produced.

    ``None`` means unknown: the probe did not run (non-chat mode, stopped
    deployment) or could not classify the deployment, and every derived key
    is omitted rather than guessed.
    """

    accepts_image: bool | None = None
    accepts_audio: bool | None = None


def facts_from_sglang_model_info(payload: object, served_task_types: frozenset[str] = frozenset()) -> CapabilityFacts:
    """Derive the fact set from an sglang ``/model_info`` response.

    Non-bool values read as unknown rather than false: a field the runtime
    omits is a fact the runtime did not compute, not a negative. A TI2I
    surface (per its own served task types) suppresses every claim, because
    its image flag describes image *editing*, not image understanding.
    """
    if not isinstance(payload, dict):
        return CapabilityFacts()
    if served_task_types and served_task_types <= _IMAGE_GEN_TASK_TYPES:
        return CapabilityFacts()

    def flag(key: str) -> bool | None:
        value = payload.get(key)
        if isinstance(value, bool):
            return value
        return None

    return CapabilityFacts(
        accepts_image=flag("has_image_understanding"),
        accepts_audio=flag("has_audio_understanding"),
    )


# HTTP verdicts that mean "the server examined the content and refused it":
# malformed-content refusals (400, 422), modality/encoding negotiation (406,
# 415), content-too-large (413). Codes outside this set say something else:
# 404 is a missing route or model id, 429 is rate limiting, 408 is a timeout,
# and none of those are verdicts about the content, so they must not freeze
# a permanent negative.
_CONTENT_REFUSAL_CODES: Final[frozenset[int]] = frozenset({400, 406, 413, 415, 422})


def probe_verdict_to_fact(status_code: int) -> bool | None:
    """Classify one synthetic-content probe's HTTP verdict.

    A 200 is acceptance (the server tokenized and processed the content), a
    content refusal is rejection (the server looked at the content and
    refused it), and anything else (5xx, 404, 429, timeout, connection error)
    is unknown: the request failed in a way that says nothing about the
    capability, so no claim is made and the probe retries next cycle.
    """
    if status_code == 200:
        return True
    if status_code in _CONTENT_REFUSAL_CODES:
        return False
    return None


def capability_metadata(mode: str, facts: CapabilityFacts | None) -> Mapping[str, object]:
    """The ``model_info`` capability keys to stamp for a discovered deployment.

    Returns an empty mapping when there is nothing safe to claim: non-chat
    modes never carry these keys (a transcription or TTS row is not a chat
    surface and its clients do not read them), and unknown facts claim
    nothing so clients keep falling back to their own conservative gating.
    """
    if mode != "chat" or facts is None:
        return {}

    metadata: dict[str, object] = {}
    if facts.accepts_image is not None:
        metadata["supports_vision"] = facts.accepts_image
    if facts.accepts_audio is not None:
        metadata["supports_audio_input"] = facts.accepts_audio
    return metadata
