"""
Lineage: the frame-aware hash chain and the immutable resolution object.

A conversation's canonical frames are folded into a running chain of sha256
digests. Each frame commits its ``frame_kind`` and ``role`` BEFORE any of its
content bytes are hashed (frame start), so two large payloads with identical
bytes but different roles diverge at the first emitted node rather than only
at the frame boundary. Large payloads are then chunked in rune-safe windows;
every frame emits a boundary checkpoint so a short new turn always advances
the chain. A terminal node anchors the whole request.

Resolution is fork-aware, replacing the old shared-prefix counter. The store
records ``chain_len`` at teach time; a lookup returning the deepest matched
node lets us classify the incoming request:

- matched_depth >= taught_chain_len - 1  ->  exact repeat or append-only
  continuation; reuse the stored session id.
- matched_depth <  taught_chain_len - 1  ->  the request diverged before the
  stored conversation ended (a fork, e.g. an unrelated chat sharing only the
  leading system prompt); mint a fresh synthetic id.

This removes the chicken-and-egg failure of counting distinct sessions on a
shared prefix: merely sharing a long beginning is never enough, the match must
reach the end of the previously taught conversation.

Only this service reads these keys, so digests are stdlib sha256.
"""

import hashlib
from dataclasses import dataclass
from typing import Final, Literal

from litellm.router_utils.session_identity.canonicalizer import canonical_frames

_UTF8_MAX_CONTINUATION: Final = 3  # utf8.UTFMax - 1: continuation bytes a boundary may skip
_MAX_DECLARED_ID_LEN: Final = 256

_FRAME_START: Final = b"frame-start"
_FRAME_CHUNK: Final = b"frame-chunk"
_FRAME_END: Final = b"frame-end"
_TERMINAL: Final = b"terminal"


def root_seed(model_group: str, cache_salt: str) -> bytes:
    """Seed scoping a chain to a model group and optional cache salt."""
    return hashlib.sha256(model_group.encode() + b"\x00" + cache_salt.encode()).digest()


def _field(raw: bytes) -> bytes:
    """One length-prefixed field: 8-byte little-endian length + bytes (unforgeable framing)."""
    return len(raw).to_bytes(8, "little") + raw


def _digest(*parts: bytes) -> bytes:
    d = hashlib.sha256()
    for part in parts:
        d.update(part)
    return d.digest()


def build_chain(
    data: dict,
    model_group: str,
    cache_salt: str,
    chunk_size: int,
) -> list[bytes]:
    """
    Running chain of digest nodes for one request body.

    Deterministic for identical input. Returns raw digest bytes (the store
    hex-encodes at the Redis boundary). No node cap: the chain length is
    bounded by the request's own content, so very long agent contexts keep
    influencing identity instead of being silently truncated.
    """
    frames = canonical_frames(data)
    if not frames or chunk_size <= 0:
        return []

    nodes: list[bytes] = []
    prev = root_seed(model_group, cache_salt)

    for frame_kind, role, payload in frames:
        # Commit the frame's identity before any content bytes so two large
        # payloads differing only in role/kind diverge at the first chunk node.
        prev = _digest(prev, _FRAME_START, _field(frame_kind.encode()), _field(role.encode()))
        nodes.append(prev)

        i = 0
        while i + chunk_size + _UTF8_MAX_CONTINUATION <= len(payload):
            end = i + chunk_size
            while end < i + chunk_size + _UTF8_MAX_CONTINUATION and payload[end] & 0xC0 == 0x80:
                end += 1
            prev = _digest(prev, _FRAME_CHUNK, payload[i:end])
            nodes.append(prev)
            i = end

        # Boundary checkpoint over the remaining (sub-chunk) tail only; the full
        # chunks above are already folded in, so the payload is never hashed twice.
        tail = payload[i:]
        prev = _digest(prev, _FRAME_END, _field(tail), len(payload).to_bytes(8, "little"))
        nodes.append(prev)

    # Terminal node: anchors the whole request so a new trailing turn moves the
    # deepest node. The frame count keeps "[a][bc]" distinct from "[a][b][c]".
    nodes.append(_digest(prev, _TERMINAL, len(frames).to_bytes(4, "little")))
    return nodes


def declared_id(data: dict) -> str | None:
    """
    The client's own name for this session, from body fields that name a
    session across turns. ``previous_response_id`` is deliberately absent: it
    names the previous turn, not the session.

    Returns the key-namespaced id so "conversation" and "prompt_cache_key"
    values cannot alias each other, or None. Long values are hashed rather than
    truncated so two ids sharing a prefix do not collapse onto one lineage.
    """
    for key in ("prompt_cache_key", "conversation"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            continue
        if len(value) > _MAX_DECLARED_ID_LEN:
            value = hashlib.sha256(value.encode()).hexdigest()
        return f"{key}\x00{value}"
    return None


def scoped_declared_session_id(declared: str, model_group: str, scope: str) -> str:
    """
    Deterministic session id for a declared conversation name. Needs no Redis:
    the same declared name re-resolves to the same id across requests and pods.
    """
    return hashlib.sha256(
        b"litellm-session-declared-v1\x00"
        + scope.encode()
        + b"\x00"
        + model_group.encode()
        + b"\x00"
        + declared.encode()
    ).hexdigest()[:32]


def synthesized_session_id(chain: list[bytes], model_group: str, scope: str) -> str:
    """
    Stable id for a fresh lineage, derived from its deepest node.

    Deterministic per lineage: the same conversation re-resolves to the same id
    across requests and pods without storing a separate id mapping.
    """
    deepest = chain[-1].hex() if chain else "empty"
    return hashlib.sha256(
        b"session_identity\x00" + deepest.encode() + b"\x00" + model_group.encode() + b"\x00" + scope.encode()
    ).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class LineageMatch:
    """A stored-lineage hit: the session id plus the geometry needed for fork detection."""

    session_id: str
    matched_depth: int  # nodes of the incoming chain matched (1-based)
    taught_chain_len: int  # nodes the stored conversation had when taught

    def is_continuation(self) -> bool:
        """
        True when the incoming request is an exact repeat or append-only
        continuation of the stored conversation: its deepest match reaches (or
        passes) the stored conversation's last content node. False means the
        request diverged before the stored conversation ended (a fork).
        """
        return self.matched_depth >= self.taught_chain_len - 1


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    """
    Per-request resolution result. Immutable and carried on the request's
    metadata (never on shared matcher state), so concurrent requests cannot
    exchange match depth.
    """

    session_id: str
    matched_depth: int
    source: Literal["explicit", "declared", "history", "synthesized", "none"]
    declared: str | None = None


def classify(chain: list[bytes], match: LineageMatch | None) -> LineageMatch | None:
    """Return ``match`` only when it represents a genuine continuation."""
    if match is None:
        return None
    return match if match.is_continuation() else None
