"""Canonical serialization of a validated request view into ordered frames.

Tools render ahead of messages, name-sorted so a reordered tools array does not
split one lineage (vLLM Router #219). Every message is framed as
``(frame_kind, role, content_bytes)``; identity-relevant fields (``name``,
``tool_call_id``, ``function_call``, ``tool_calls``) each become their own frame.
Non-JSON-native values raise (fail-open at the resolver) rather than coerce.
"""

from collections.abc import Iterator
from itertools import chain as _flatten
from typing import Final, TypeAlias

import orjson

from litellm.router_utils.session_identity.lineage import Frame
from litellm.router_utils.session_identity.views import MessageView, RequestView

_ORJSON_OPTIONS: Final = orjson.OPT_SORT_KEYS | orjson.OPT_NAIVE_UTC

FrameSeq: TypeAlias = tuple[Frame, ...]


def _json_bytes(value: object) -> bytes:
    return orjson.dumps(value, option=_ORJSON_OPTIONS)


def _tool_sort_key(tool: object) -> tuple[str, str]:
    if isinstance(tool, dict):
        ttype: Final = str(tool.get("type", ""))
        fn: Final = tool.get("function")
        name: Final = str(fn.get("name", "")) if isinstance(fn, dict) else ""
        return (ttype, name)
    return ("", "")


def _sorted_tools(tools: object) -> tuple[object, ...]:
    """Tools name-sorted so array reordering does not split a lineage. Sorting
    reads only type/name; the canonical bytes are dumped once by the caller, not
    per-tool inside the sort key."""
    if not isinstance(tools, list):
        return (tools,)
    return tuple(sorted(tools, key=_tool_sort_key))


def _content_bytes(content: object) -> bytes:
    if isinstance(content, str):
        return content.encode()
    if content is None:
        return b""
    return _json_bytes(content)


def _message_frames(message: MessageView) -> Iterator[Frame]:
    role: Final = message.get("role", "")
    name: Final = message.get("name")
    if name:
        yield ("name", role, str(name).encode())
    yield ("msg", role, _content_bytes(message.get("content")))
    tool_call_id: Final = message.get("tool_call_id")
    if tool_call_id:
        yield ("tool_call_id", role, str(tool_call_id).encode())
    tool_calls: Final = message.get("tool_calls")
    if tool_calls:
        yield ("msg", role + "/tool_calls", _json_bytes(tool_calls))
    function_call: Final = message.get("function_call")
    if function_call:
        yield ("msg", role + "/function_call", _json_bytes(function_call))


def canonical_frames(request: RequestView) -> FrameSeq:
    """Ordered canonical frames in engine order: tools first, then messages."""
    tools: Final = request.get("tools")
    tool_frames: Final[tuple[Frame, ...]] = (
        (("tools", "", _json_bytes(_sorted_tools(tools))),) if tools else ()
    )
    messages: Final = request.get("messages") or ()
    message_frames: Final = _flatten.from_iterable(_message_frames(m) for m in messages)
    return tool_frames + tuple(message_frames)
