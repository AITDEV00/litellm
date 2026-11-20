"""Shared serializer/hydrator for transcription responses.

One converter for every final JSON key, used by both the cold provider path
(the config's ``transform_audio_transcription_response`` writing provider
payload fields onto the response object) and the cache read path
(``convert_to_model_response_object`` with ``response_type='audio_transcription'``).

Why this exists: the previous cache-side converter rebuilt only a fixed allowlist
(``text``, ``language``, ``task``, ``duration``, ``words``, ``segments``) plus a
recognized ``usage``. A native provider payload carrying e.g.
``schema_version``, ``diarization_timestamps`` or ``transcript_origin`` survived
the first (cold) HTTP response but was silently narrowed on the second (cached)
response, so two identical requests returned different public JSON.

The round-trip contract:

- every JSON-compatible public value set on a ``TranscriptionResponse`` is
  preserved, including unrecognized native extension fields
- recognized transcription usage objects (``type: duration|tokens``) are
  validated through the existing pydantic models so cost calculation stays typed
- an unrecognized usage shape stays as public metadata rather than rejecting the
  whole cached response; it is explicitly not coerced into a cost object
- internal keys (``_hidden_params``, provider URLs, cache artifacts) never leak
  into the public JSON object
"""

import json
from collections.abc import Mapping
from typing import Final, TypeAlias, cast

from litellm.types.utils import (
    TranscriptionResponse,
    TranscriptionUsageDurationObject,
    TranscriptionUsageTokensObject,
)

_USAGE_FIELD: Final = "usage"
_AUDIO_DURATION_INTERNAL_KEY: Final = "_audio_transcription_duration"
_AUDIO_DURATION_HIDDEN_KEY: Final = "audio_transcription_duration"

_RecognizedUsage: TypeAlias = TranscriptionUsageDurationObject | TranscriptionUsageTokensObject


def _recognized_transcription_usage(value: object) -> _RecognizedUsage | None:
    """Validate a usage mapping through the typed models, or None if unrecognized.

    An unrecognized shape is preserved passthrough by the caller; returning
    None here never rejects the surrounding response.
    """
    if not isinstance(value, Mapping):
        return None
    usage_type: Final = value.get("type")
    try:
        if usage_type == "duration":
            return TranscriptionUsageDurationObject(**dict(value))
        if usage_type == "tokens":
            return TranscriptionUsageTokensObject(**dict(value))
    except Exception:
        return None
    return None


def _json_compatible(value: object) -> bool:
    """Whether the value can survive a cache JSON round-trip unchanged.

    Unknown provider fields ride the response only if they can be serialized
    and restored losslessly; a non-JSON value (e.g. a live file object) is
    dropped rather than crashing the cache write or the public dump.
    """
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def transcription_public_payload(response: TranscriptionResponse) -> dict[str, object]:
    """Serialize the public (client-visible) view of a transcription response.

    Includes every dynamic public attribute set by the provider transform, not
    just the declared model fields, so a cold HTTP response and its cached
    copy produce the same public JSON. Internal underscore-prefixed attributes
    are excluded: ``_hidden_params`` and friends are server-side metadata.
    """
    payload: Final[dict[str, object]] = {}
    for key, value in response.model_dump().items():
        if key.startswith("_"):
            continue
        payload[key] = value
    return payload


def hydrate_transcription_response(
    response_object: Mapping[str, object],
    model_response: TranscriptionResponse | None = None,
    hidden_params: Mapping[str, object] | None = None,
    response_headers: Mapping[str, str] | None = None,
) -> TranscriptionResponse:
    """Hydrate a ``TranscriptionResponse`` from a provider/cache payload mapping.

    This is the single shared converter for final JSON keys: the same one that
    reads a cached dict rehydrates the fields a provider response carries, so
    the two paths can never drift apart again.

    ``response_object`` keys:

    - ``text`` and any dynamic public field are copied onto the response
    - ``usage`` is validated through the typed duration/tokens models when it
      matches one; an unrecognized shape is kept as public passthrough metadata
    - ``_audio_transcription_duration`` (an internal-only key) is routed to
      hidden params, never to the public body
    """
    response: Final[TranscriptionResponse] = model_response if model_response is not None else TranscriptionResponse()

    response.text = str(response_object.get("text") or "")

    for key, value in response_object.items():
        if key == "text" or key == _USAGE_FIELD or key == _AUDIO_DURATION_INTERNAL_KEY or key.startswith("_"):
            continue
        if value is None or not _json_compatible(value):
            continue
        setattr(response, key, value)

    usage_value: Final = response_object.get(_USAGE_FIELD)
    recognized_usage: Final = _recognized_transcription_usage(usage_value)
    if recognized_usage is not None:
        response.usage = recognized_usage
    elif usage_value is not None and _json_compatible(usage_value):
        setattr(response, _USAGE_FIELD, dict(usage_value) if isinstance(usage_value, Mapping) else usage_value)

    if hidden_params is not None:
        response.hidden_params = dict(hidden_params)

    internal_duration: Final = response_object.get(_AUDIO_DURATION_INTERNAL_KEY)
    if internal_duration is not None:
        response.hidden_params[_AUDIO_DURATION_HIDDEN_KEY] = internal_duration

    if response_headers is not None:
        response._response_headers = dict(response_headers)

    return response
