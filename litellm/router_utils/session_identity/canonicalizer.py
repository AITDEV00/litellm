"""
Canonical serialization of a Chat Completions request into ordered frames.

The single canonical representation of a request; the lineage chain is built
directly from these frames, so there is no second byte-stream implementation to
drift apart. The frames must be stable across requests of the same conversation
and must move when the conversation grows:

- tools render ahead of the messages, name-sorted so a reordered tools array
  does not split one lineage (vLLM Router #219); each tool's own keys are
  sorted for determinism
- every message is framed as ``(frame_kind, role, content_bytes)``; the chain
  commits frame_kind+role before content, so role A + content B can never run
  into role A + content BC as the same node
- semantically relevant identity fields (``name``, ``tool_call_id``, legacy
  ``function_call``, ``tool_calls``) each become their own frame, so two tool
  results that differ only in ``tool_call_id`` do not canonicalize identically
- structured content blocks render via sorted-key JSON

Framing is length-prefixed (``struct.Struct("<Q")``), not delimiter-terminated:
a client embedding a delimiter in its own text cannot mint a fake boundary.
Uses orjson (a prod dependency) with sorted keys. Non-JSON-native values raise
(fail-open at the resolver), rather than being coerced with ``str()`` whose
representation can vary across runs and silently change identity.
"""

from struct import Struct
from typing import Any, Final

import orjson

_ORJSON_OPTIONS: Final = orjson.OPT_SORT_KEYS | orjson.OPT_NAIVE_UTC
_U64: Final = Struct("<Q")

Frame = tuple[str, str, bytes]


def field_bytes(value: bytes) -> bytes:
    """One length-prefixed field: 8-byte little-endian length + value."""
    return _U64.pack(len(value)) + value


def _sorted_tools(tools: Any) -> list[Any]:
    """Tools name-sorted so array reordering does not split a lineage."""
    if not isinstance(tools, list):
        return [tools]

    def sort_key(tool: Any) -> tuple[str, str, str]:
        if isinstance(tool, dict):
            ttype = str(tool.get("type", ""))
            fn = tool.get("function")
            name = str(fn.get("name", "")) if isinstance(fn, dict) else ""
            blob = orjson.dumps(tool, option=_ORJSON_OPTIONS).decode()
            return (ttype, name, blob)
        return ("", "", orjson.dumps(tool, option=_ORJSON_OPTIONS).decode())

    return sorted(tools, key=sort_key)


def _json_bytes(value: Any) -> bytes:
    return orjson.dumps(value, option=_ORJSON_OPTIONS)


def _content_bytes(content: Any) -> bytes:
    """Content of a message; structured blocks render as sorted-key JSON."""
    if isinstance(content, str):
        return content.encode()
    if content is None:
        return b""
    return _json_bytes(content)


def canonical_frames(data: dict) -> list[Frame]:
    """
    Request as ordered canonical frames ``[(frame_kind, role, content_bytes)]``
    in engine order: tools first, then messages. Raises (fail-open upstream) on
    non-JSON-native values rather than coercing them.
    """
    frames: list[Frame] = []

    tools = data.get("tools")
    if tools:
        frames.append(("tools", "", _json_bytes(_sorted_tools(tools))))

    messages = data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", ""))
            name = message.get("name")
            if name:
                frames.append(("name", role, str(name).encode()))
            frames.append(("msg", role, _content_bytes(message.get("content"))))

            tool_call_id = message.get("tool_call_id")
            if tool_call_id:
                frames.append(("tool_call_id", role, str(tool_call_id).encode()))

            tool_calls = message.get("tool_calls")
            if tool_calls:
                frames.append(("msg", role + "/tool_calls", _json_bytes(tool_calls)))

            function_call = message.get("function_call")
            if function_call:
                frames.append(("msg", role + "/function_call", _json_bytes(function_call)))
    return frames
