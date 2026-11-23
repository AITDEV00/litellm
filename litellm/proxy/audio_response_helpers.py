"""Shared final-response handling for multipart audio proxy routes.

Both ``/v1/audio/transcriptions`` and ``/v1/audio/translations`` finish with
the same contract: custom headers computed from hidden params, the response
headers hook, and a body that depends on the response medium. JSON results
return the model's public JSON; a ``NativeAudioDocumentResponse`` returns the
raw document bytes under the provider's declared media type. One helper keeps
the two endpoints from drifting apart and keeps plaintext responses from
becoming a JSON envelope with a ``text`` field.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Final

from fastapi import Response
from starlette.responses import JSONResponse, StreamingResponse

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing
from litellm.types.llms.audio_transcription import (
    NativeAudioDocumentResponse,
    NativeTranscriptDelta,
    NativeTranscriptDone,
    NativeTranscriptEvent,
    NativeTranscriptStreamError,
    OpenedNativeAudioStream,
)
from litellm.types.utils import TranscriptionResponse

if TYPE_CHECKING:
    from litellm.proxy.utils import ProxyLogging


def _hidden_params_of(response: TranscriptionResponse | NativeAudioDocumentResponse) -> dict[str, object]:
    return getattr(response, "_hidden_params", {}) or {}


def build_audio_custom_headers(
    *,
    response: TranscriptionResponse | NativeAudioDocumentResponse,
    data: dict,
    user_api_key_dict: UserAPIKeyAuth,
    version: str,
    request_data: dict,
) -> dict[str, str]:
    """Compute the standard custom headers for an audio response.

    Reads explicit hidden-param metadata (model_id, cache_key, api_base,
    response_cost, call id, additional headers) the same way for every
    response medium, so the document and stream branches cannot silently skip
    cost or cache facts.
    """
    hidden_params: Final = _hidden_params_of(response)
    model_id: Final = hidden_params.get("model_id", None) or ""
    cache_key: Final = hidden_params.get("cache_key", None) or ""
    api_base: Final = hidden_params.get("api_base", None) or ""
    response_cost: Final = hidden_params.get("response_cost", None) or ""
    response_call_id: Final = hidden_params.get("litellm_call_id", None) or ""
    additional_headers: Final = hidden_params.get("additional_headers", {}) or {}

    return ProxyBaseLLMRequestProcessing.get_custom_headers(
        user_api_key_dict=user_api_key_dict,
        model_id=model_id,
        cache_key=cache_key,
        api_base=api_base,
        version=version,
        response_cost=response_cost,
        model_region=getattr(user_api_key_dict, "allowed_model_region", ""),
        call_id=response_call_id,
        request_data=request_data,
        hidden_params=hidden_params,
        **additional_headers,
    )


async def apply_audio_response_headers_hook(
    *,
    proxy_logging_obj: ProxyLogging,
    data: dict,
    user_api_key_dict: UserAPIKeyAuth,
    response: TranscriptionResponse | NativeAudioDocumentResponse,
    request_headers: dict[str, str],
) -> dict[str, str]:
    """Run ``post_call_response_headers_hook`` for both response shapes."""
    callback_headers = await proxy_logging_obj.post_call_response_headers_hook(
        data=data,
        user_api_key_dict=user_api_key_dict,
        response=response,
        request_headers=request_headers,
    )
    return callback_headers or {}


async def apply_audio_post_call_processing(
    *,
    proxy_logging_obj: ProxyLogging,
    data: dict,
    user_api_key_dict: UserAPIKeyAuth,
    response: TranscriptionResponse | NativeAudioDocumentResponse,
) -> TranscriptionResponse | NativeAudioDocumentResponse:
    processed: Final = await proxy_logging_obj.post_call_success_hook(
        data=data,
        user_api_key_dict=user_api_key_dict,
        response=response,
    )
    if isinstance(processed, (TranscriptionResponse, NativeAudioDocumentResponse)):
        return processed
    if isinstance(processed, Mapping) and isinstance(processed.get("text"), str):
        from litellm.litellm_core_utils.llm_response_utils.transcriptions import hydrate_transcription_response

        return hydrate_transcription_response(
            response_object=processed,
            hidden_params=response.hidden_params,
            response_headers=getattr(response, "_response_headers", None),
        )
    raise TypeError(f"Audio post-call processing returned an unsupported result: {type(processed).__name__}")


async def reject_incompatible_native_transcript_guardrails(*, data: dict) -> None:
    import litellm
    from litellm.exceptions import BadRequestError
    from litellm.integrations.custom_guardrail import CustomGuardrail
    from litellm.proxy import proxy_server
    from litellm.proxy.utils import check_and_merge_model_level_guardrails, pipeline_managed_guardrail_names
    from litellm.types.guardrails import GuardrailEventHooks

    guardrail_data: Final = check_and_merge_model_level_guardrails(data=data, llm_router=proxy_server.llm_router)
    if pipeline_managed_guardrail_names(guardrail_data, "post_call"):
        raise BadRequestError(
            message=(
                "Streaming native transcripts cannot apply post-call guardrail pipelines; "
                "resend with stream=false for whole-transcript processing"
            ),
            model=str(data.get("model") or ""),
            llm_provider="",
        )
    configured: Final = tuple(
        callback.guardrail_name
        for callback in litellm.callbacks
        if isinstance(callback, CustomGuardrail)
        and callback.should_run_guardrail(data=guardrail_data, event_type=GuardrailEventHooks.post_call)
    )
    if not configured:
        return
    raise BadRequestError(
        message=(
            f"Streaming native transcripts cannot apply whole-response guardrails {configured}; "
            "resend with stream=false so LiteLLM can inspect the complete transcript before returning it"
        ),
        model=str(data.get("model") or ""),
        llm_provider="",
    )


def audio_json_response(
    response: TranscriptionResponse | Mapping[str, object],
    *,
    headers: dict[str, str],
) -> Response:
    """Return a transcription/translation JSON body.

    ``model_dump`` carries the dynamic public fields the provider set (the
    shared hydrator guarantees the cached copy has them too), so the cold and
    cached responses are the same public JSON. No ``exclude_none``: the
    previous FastAPI-serialized return kept declared-but-unset fields (e.g.
    ``usage: null``), and the public schema must not change under this
    refactor. A plain mapping (a test double or a pass-through cached dict) is
    returned as-is.
    """
    payload: Final[object] = response.model_dump() if isinstance(response, TranscriptionResponse) else dict(response)
    return JSONResponse(content=payload, headers=headers)


def audio_document_response(
    response: NativeAudioDocumentResponse,
    *,
    headers: dict[str, str],
) -> Response:
    """Return true document bytes (text/SRT/VTT) under the native media type.

    The body is the document itself, never ``{"text": ...}``; the media type is
    the one the provider declared (or the derived default for a bare 200
    text/plain), and private transport metadata stays out of the body.
    """
    charset: Final = response.charset or "utf-8"
    media_type: Final = f"{response.media_type}; charset={charset}"
    return Response(
        content=response.body.encode(charset),
        media_type=media_type,
        headers=headers,
    )


def is_opened_native_audio_stream(response: object) -> bool:
    return isinstance(response, OpenedNativeAudioStream)


def build_audio_stream_custom_headers(
    *,
    stream: OpenedNativeAudioStream,
    user_api_key_dict: UserAPIKeyAuth,
    version: str,
    request_data: dict,
) -> dict[str, str]:
    """Custom headers for a native transcript stream, before first iteration.

    Same explicit hidden-param reads as ``build_audio_custom_headers`` (the
    stream handle carries the same surface), so header commit happens with
    real model/cost facts while the transport is still open and no event has
    been consumed.
    """
    hidden_params: Final[dict[str, object]] = stream.hidden_params or {}
    model_id: Final = hidden_params.get("model_id", None) or ""
    cache_key: Final = hidden_params.get("cache_key", None) or ""
    api_base: Final = hidden_params.get("api_base", None) or ""
    response_cost: Final = hidden_params.get("response_cost", None) or ""
    response_call_id: Final = hidden_params.get("litellm_call_id", None) or ""
    additional_headers: Final = hidden_params.get("additional_headers", {}) or {}

    return ProxyBaseLLMRequestProcessing.get_custom_headers(
        user_api_key_dict=user_api_key_dict,
        model_id=model_id,
        cache_key=cache_key,
        api_base=api_base,
        version=version,
        response_cost=response_cost,
        model_region=getattr(user_api_key_dict, "allowed_model_region", ""),
        call_id=response_call_id,
        request_data=request_data,
        hidden_params=hidden_params,
        **additional_headers,
    )


def serialize_native_transcript_event(event: NativeTranscriptEvent) -> str:
    """Deterministic SSE frame for one typed transcript event.

    The public frame keeps the provider's event vocabulary (``event:`` name
    and the native ``type`` field) plus every native field the provider sent,
    so gateway clients see the provider's stream, not an invented dialect.
    """
    match event:
        case NativeTranscriptDelta():
            event_name: Final = "transcript.text.delta"
        case NativeTranscriptDone():
            event_name = "transcript.text.done"
        case NativeTranscriptStreamError():
            event_name = "error"
    payload: Final = dict(event.native_event or {})
    if isinstance(event, NativeTranscriptStreamError):
        payload.setdefault("message", event.message)
    return f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def _native_transcript_sse_body(
    stream: OpenedNativeAudioStream,
) -> AsyncIterator[bytes]:
    """Serialize each typed event, then close the stream exactly once.

    The route's StreamingResponse holds this generator open; when the client
    disconnects, starlette closes the generator (GeneratorExit inside the
    async generator), which the shared stream turns into upstream transport
    cancellation with no terminal logging.
    """
    try:
        async for event in stream.events:
            yield serialize_native_transcript_event(event).encode("utf-8")
    finally:
        await stream.aclose()


def audio_stream_response(
    stream: OpenedNativeAudioStream,
    *,
    headers: dict[str, str],
) -> StreamingResponse:
    """Return a ``text/event-stream`` response over the opened native stream.

    ``background`` is not used for terminal logging: the stream's own
    terminal event dispatches success/failure exactly once when the provider
    answers, and repeated generator closure only cancels the transport.
    """
    return StreamingResponse(
        content=_native_transcript_sse_body(stream),
        media_type="text/event-stream",
        headers=headers,
    )


__all__ = [
    "apply_audio_post_call_processing",
    "apply_audio_response_headers_hook",
    "audio_document_response",
    "audio_json_response",
    "audio_stream_response",
    "build_audio_custom_headers",
    "build_audio_stream_custom_headers",
    "is_opened_native_audio_stream",
    "reject_incompatible_native_transcript_guardrails",
    "serialize_native_transcript_event",
]
