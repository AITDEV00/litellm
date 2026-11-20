"""Cold-vs-cache round-trip tests for the shared transcription hydrator.

The gateway's first (cold) HTTP response carries every native public field the
provider set. The cached copy flows back through
``convert_to_model_response_object(response_type='audio_transcription')``.
Before the shared hydrator existed, that converter rebuilt only a fixed
allowlist, so the second response was silently narrower than the first. These
tests pin the invariant the plan requires: identical public JSON on cold and
cached responses, hidden/internal keys never leaking into the public body.
"""

from typing import Final

import pytest

from litellm.litellm_core_utils.llm_response_utils.transcriptions import (
    hydrate_transcription_response,
    transcription_public_payload,
)
from litellm.types.utils import TranscriptionResponse

_RICH_NATIVE_PAYLOAD: Final[dict[str, object]] = {
    "text": "hello world",
    "language": "en",
    "task": "transcribe",
    "duration": 3.2,
    "schema_version": "1.0",
    "word_timestamp_alignment": True,
    "transcript_origin": "asmr",
    "diarization_frame_seconds": 0.08,
    "diarization_timestamps": [{"speaker": 1, "start": 0.0, "end": 1.2}],
    "speaker_word_timestamps": {"1": [{"word": "hello", "start": 0.0, "end": 0.5}]},
    "subtitle_cues": [{"start": 0, "end": 1, "text": "hello"}],
    "words": [{"word": "hello", "start": 0.0, "end": 0.5}],
    "usage": {"type": "duration", "seconds": 3.2},
}


def _cold_response() -> TranscriptionResponse:
    """The response a provider transform builds from the rich payload."""
    response = TranscriptionResponse()
    for key, value in _RICH_NATIVE_PAYLOAD.items():
        setattr(response, key, value)
    return response


def _cached_response() -> TranscriptionResponse:
    """The response the cache-read path rebuilds from the same payload."""
    return hydrate_transcription_response(response_object=_RICH_NATIVE_PAYLOAD)


def test_cold_and_cached_public_json_are_identical() -> None:
    cold = transcription_public_payload(_cold_response())
    cached = transcription_public_payload(_cached_response())
    assert cold == cached, "cached response narrowed the public JSON"
    for key in _RICH_NATIVE_PAYLOAD:
        assert key in cached, f"{key} dropped from the cached public JSON"


def test_hydrator_preserves_every_json_public_value() -> None:
    response = _cached_response()
    assert response.text == "hello world"
    assert response["schema_version"] == "1.0"
    assert response["diarization_frame_seconds"] == 0.08
    assert response["word_timestamp_alignment"] is True
    assert response["diarization_timestamps"] == [{"speaker": 1, "start": 0.0, "end": 1.2}]


def test_hydrator_validates_recognized_usage_types() -> None:
    duration = hydrate_transcription_response(
        response_object={"text": "hi", "usage": {"type": "duration", "seconds": 3.2}}
    )
    assert duration.usage is not None
    assert duration.usage.seconds == 3.2

    tokens = hydrate_transcription_response(
        response_object={
            "text": "hi",
            "usage": {
                "type": "tokens",
                "input_tokens": 10,
                "output_tokens": 4,
                "total_tokens": 14,
                "input_token_details": {"audio_tokens": 8, "text_tokens": 2},
            },
        }
    )
    assert tokens.usage is not None
    assert tokens.usage.input_tokens == 10


def test_hydrator_keeps_unrecognized_usage_passthrough() -> None:
    """An unknown native usage shape must not reject the whole response."""
    response = hydrate_transcription_response(
        response_object={"text": "hi", "usage": {"units": "audio_seconds", "count": 3}}
    )
    assert response.text == "hi"
    assert response["usage"] == {"units": "audio_seconds", "count": 3}


def test_hydrator_never_leaks_hidden_params_into_public_json() -> None:
    hidden: Final[dict[str, object]] = {
        "model_id": "dep-123",
        "response_cost": 0.01,
        "api_base": "http://internal:8000/v1",
        "additional_headers": {"x-internal": "secret"},
        "cache_key": "k",
    }
    response = hydrate_transcription_response(
        response_object=_RICH_NATIVE_PAYLOAD,
        hidden_params=hidden,
    )
    public = transcription_public_payload(response)
    for key in ("model_id", "response_cost", "api_base", "additional_headers", "cache_key"):
        assert key not in public, f"internal {key} leaked into the public JSON"
    assert response.hidden_params == hidden


def test_hydrator_routes_internal_duration_to_hidden_params() -> None:
    """``_audio_transcription_duration`` is an internal-only key: it feeds cost
    calculation through hidden params, never the public body."""
    response = hydrate_transcription_response(
        response_object={**_RICH_NATIVE_PAYLOAD, "_audio_transcription_duration": 4.0},
    )
    public = transcription_public_payload(response)
    assert "_audio_transcription_duration" not in public
    assert "audio_transcription_duration" not in public
    assert response.hidden_params["audio_transcription_duration"] == 4.0


def test_hydrator_drops_unserializable_values_not_the_response() -> None:
    class Unserializable:
        pass

    response = hydrate_transcription_response(
        response_object={"text": "hi", "bad_field": Unserializable()},
    )
    assert response.text == "hi"
    assert "bad_field" not in transcription_public_payload(response)


def test_hydrator_applies_response_headers() -> None:
    response = hydrate_transcription_response(
        response_object={"text": "hi"},
        response_headers={"x-request-id": "req-1"},
    )
    assert response._response_headers == {"x-request-id": "req-1"}


def test_hydrator_reuses_passed_response_object() -> None:
    """A pre-constructed response object is hydrated in place, not replaced."""
    existing = TranscriptionResponse()
    result = hydrate_transcription_response(
        response_object={"text": "hi"},
        model_response=existing,
    )
    assert result is existing
    assert result.text == "hi"


def test_hydrator_defaults_missing_text_to_empty_string() -> None:
    response = hydrate_transcription_response(response_object={"usage": {"type": "duration", "seconds": 1.0}})
    assert response.text == ""


@pytest.mark.parametrize("usage", (None, {"type": "unknown"}, {"type": "duration", "seconds": None}))
def test_hydrator_tolerates_degenerate_usage(usage: object) -> None:
    response = hydrate_transcription_response(response_object={"text": "hi", "usage": usage})
    assert response.text == "hi"
