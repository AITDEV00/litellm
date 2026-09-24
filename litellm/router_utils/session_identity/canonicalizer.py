"""
Canonical serialization of a Chat Completions request into the byte stream the
hash chain is built over.

Port of the llm-d contentStream framing (chunk.go), reduced to the OpenAI chat
surface LiteLLM routes. The stream must be stable across requests of the same
conversation and must move when the conversation grows:

- tools render ahead of the messages, in the order the engine sees them, with
  sorted-key JSON so tool-schema dict ordering cannot split one lineage in two
- every message is framed as surface, role, then content, then tool_calls, so
  "role A + content B" can never run into "role A + content BC" as the same
  bytes
- image/detail and other structured content blocks render via their stable
  sorted-key JSON

Uses orjson (a prod dependency, ~4x faster than stdlib json.dumps for large
payloads) with OPT_SORT_KEYS for deterministic ordering, mirroring
json.dumps(..., sort_keys=True).
"""

import orjson
from typing import Any, Final

_ORJSON_OPTIONS: Final = orjson.OPT_SORT_KEYS | orjson.OPT_NAIVE_UTC


def _field(buf: bytearray, s: str) -> None:
    """
    One length-prefixed field, mirroring llm-d's field() (chunk.go).

    Framing is length-prefixed, not delimiter-terminated: a client that embeds
    a NUL (or any other delimiter) in its own message text could otherwise mint
    a fake frame boundary and make its request hash as if it continued another
    session's history. Length-prefixing makes the boundary unforgeable
    regardless of what bytes the client sends.
    """
    encoded = s.encode()
    buf.extend(len(encoded).to_bytes(10, "little"))  # varint slot, same role as Go's binary.PutUvarint buffer
    buf.extend(encoded)


def _seg(buf: bytearray, surface: str, role: str, text: str) -> None:
    """Framed segment: surface, role, text as three length-prefixed fields, mirroring llm-d's seg()."""
    _field(buf, surface)
    _field(buf, role)
    _field(buf, text)


def _default(value: Any) -> str:
    """Fallback for exotic values (datetime, Decimal, ...), mirroring default=str."""
    return str(value)


def _seg_json(buf: bytearray, surface: str, role: str, value: Any) -> None:
    """JSON segment with sorted keys, mirroring llm-d's segJSON()."""
    if value is None:
        return
    _seg(buf, surface, role, orjson.dumps(value, option=_ORJSON_OPTIONS, default=_default).decode())


def _content_text(content: Any) -> str | None:
    """Text of a message content field; None when it is structured."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, (list, dict)):
        return None  # structured blocks render as JSON below
    return str(content)


def canonicalize_chat(data: dict) -> bytes:
    """
    Byte stream for a Chat Completions request body.

    ``data`` is the proxy request dict: ``messages``, ``tools`` and any
    extra body fields the caller declared (prompt_cache_key is deliberately
    excluded - it names the session, it must not decide the chain).
    """
    buf = bytearray()
    tools = data.get("tools")
    if tools:
        _seg_json(buf, "chat", "tools", tools)

    messages = data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", ""))
            content = message.get("content")
            text = _content_text(content)
            if text is None:
                _seg_json(buf, "chat", role, content)
            else:
                _seg(buf, "chat", role, text)
            tool_calls = message.get("tool_calls")
            if tool_calls:
                _seg_json(buf, "chat", role + "/tool_calls", tool_calls)
    return bytes(buf)


def canonical_frames(data: dict) -> list[tuple[str, str, bytes]]:
    """
    Request as ordered canonical frames ``[(frame_kind, role, content_bytes)]``
    for the frame-aware hash chain. Tools first, then messages, matching the
    engine's view. Content bytes are the same framed segments
    ``canonicalize_chat`` produces per unit, but emitted per logical frame so
    the chain can checkpoint message boundaries.
    """
    frames: list[tuple[str, str, bytes]] = []
    tools = data.get("tools")
    if tools:
        frames.append(("tools", "", orjson.dumps(tools, option=_ORJSON_OPTIONS, default=_default)))

    messages = data.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role", ""))
            content = message.get("content")
            text = _content_text(content)
            payload = text.encode() if isinstance(text, str) else orjson.dumps(content, option=_ORJSON_OPTIONS, default=_default)
            frames.append(("msg", role, payload))
            tool_calls = message.get("tool_calls")
            if tool_calls:
                frames.append(("msg", role + "/tool_calls", orjson.dumps(tool_calls, option=_ORJSON_OPTIONS, default=_default)))
    return frames
