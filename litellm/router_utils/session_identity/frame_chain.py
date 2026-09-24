"""
Message-frame-aware hash chain for session identity.

Extends the raw byte chunk chain (``hash_chain.chunk_chain``) with logical
message-boundary checkpoints. The raw chain drops a trailing partial chunk
(below ``chunk_size`` + UTF-8 reserve), which is correct for estimating
reusable KV bytes but wrong for *identity*: a 30-byte new user turn must still
produce a fresh distinguishing node or two conversations that differ only in
their latest short turn are indistinguishable until one grows past the chunk
threshold.

The chain is built over canonical *frames* (``canonicalizer.canonical_frames``)
rather than one flat byte stream:

- each frame streams through the byte-chunk machinery in ``chunk_size``
  windows, so a large system prompt still yields multiple nodes
- at every message boundary we emit a checkpoint digest over
  ``(prev_state, frame_kind, role, frame_content_digest, next_boundary)``,
  which guarantees a stable node even for a sub-chunk frame
- the final node is always a boundary checkpoint of the whole request, so two
  conversations differing only in the latest turn diverge at their tail

Framing uses NUL delimiters everywhere, as in ``hash_chain``, so
("system", "ab") never collides with ("systemab", "").
"""

import hashlib
from typing import Final

from litellm.router_utils.session_identity.hash_chain import root_seed

_FRAME_TOOLS: Final = b"tools"
_FRAME_MESSAGE: Final = b"msg"


def _field(s: str) -> bytes:
    """Length-prefixed field (llm-d chunk.go field()): unforgeable framing."""
    encoded = s.encode()
    return len(encoded).to_bytes(10, "little") + encoded


def _digest(*parts: bytes) -> bytes:
    d = hashlib.sha256()
    for part in parts:
        d.update(part)
    return d.digest()


def frame_chain(
    frames: list[tuple[str, str, bytes]],
    seed: bytes,
    chunk_size: int,
    max_nodes: int,
) -> list[str]:
    """
    Nodes for one request.

    ``frames`` is ``[(frame_kind, role, content_bytes), ...]`` in engine order
    (tools first, then messages). ``chunk_size`` windows large frame payloads;
    every frame boundary emits a checkpoint regardless of payload size, so a
    short new turn always advances the chain. Returns hex digests, capped at
    ``max_nodes``.
    """
    if chunk_size <= 0 or max_nodes <= 0 or not frames:
        return []

    nodes: list[str] = []
    prev = seed

    def emit(node: bytes) -> bool:
        nodes.append(node.hex())
        return len(nodes) < max_nodes

    for frame_kind, role, payload in frames:
        if len(payload) > chunk_size:
            # chunk the large payload in rune-safe windows (same reserve rule
            # as hash_chain.chunk_chain)
            i = 0
            while i + chunk_size + 3 <= len(payload):
                end = i + chunk_size
                while end < i + chunk_size + 3 and payload[end] & 0xC0 == 0x80:
                    end += 1
                prev = _digest(prev, payload[i:end])
                if not emit(prev):
                    return nodes
                i = end
            # remainder becomes part of the boundary checkpoint below
            boundary = _digest(
                prev,
                _field(frame_kind),
                _field(role),
                payload,
            )
        else:
            boundary = _digest(
                prev,
                _field(frame_kind),
                _field(role),
                payload,
            )
        prev = boundary
        if not emit(prev):
            return nodes

    # terminal node: anchors the whole request so a new trailing turn always
    # moves the deepest node
    terminal = _digest(prev, b"end", len(frames).to_bytes(4, "little"))
    nodes.append(terminal.hex())
    return nodes
