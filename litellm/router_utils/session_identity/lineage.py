"""
Lineage: the frame-aware hash chain and the immutable resolution object.

A conversation's canonical frames are folded into a running chain of sha256
digests. Each frame commits its ``frame_kind`` and ``role`` BEFORE any of its
content bytes are hashed (frame start), so two large payloads with identical
bytes but different roles diverge at the first emitted node rather than only
at the frame boundary. Large payloads are then chunked in rune-safe windows;
every frame emits a boundary checkpoint so a short new turn always advances
the chain. A terminal node anchors the whole request.

Resolution is fork-aware. The store records ``chain_len`` at teach time; a
lookup returning the deepest matched node classifies the incoming request:

- matched_depth >= taught_chain_len - 1  ->  exact repeat or append-only
  continuation; reuse the stored session id.
- matched_depth <  taught_chain_len - 1  ->  the request diverged before the
  stored conversation ended (a fork); mint a fresh synthetic id.

The chain is built functionally: each frame expands to an immutable stream of
hash events, folded left-to-right with ``itertools.accumulate`` over a single
immutable accumulator. No local mutation or rebinding. Only this service reads
these keys, so digests are stdlib sha256.
"""

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import accumulate
from itertools import chain as _flatten
from typing import Final, Literal, TypeAlias

from litellm.router_utils.session_identity.views import RequestView

_UTF8_MAX_CONTINUATION: Final = 3  # utf8.UTFMax - 1: continuation bytes a boundary may skip
_MAX_DECLARED_ID_LEN: Final = 256

_FRAME_START: Final = b"frame-start"
_FRAME_CHUNK: Final = b"frame-chunk"
_FRAME_END: Final = b"frame-end"
_TERMINAL: Final = b"terminal"

# A hash event: the digest parts folded into the running state for one node.
HashEvent: TypeAlias = tuple[bytes, ...]
Frame: TypeAlias = tuple[str, str, bytes]


def root_seed(model_group: str, cache_salt: str) -> bytes:
    """Seed scoping a chain to a model group and optional cache salt."""
    return hashlib.sha256(model_group.encode() + b"\x00" + cache_salt.encode()).digest()


def _field(raw: bytes) -> bytes:
    """One length-prefixed field: 8-byte little-endian length + bytes (unforgeable framing)."""
    return len(raw).to_bytes(8, "little") + raw


def _fold(state: bytes, event: HashEvent) -> bytes:
    d: Final = hashlib.sha256()
    d.update(state)
    for part in event:
        d.update(part)
    return d.digest()


def _chunk_bounds(payload: bytes, chunk_size: int) -> tuple[tuple[int, int], ...]:
    """(start, end) byte windows for each full rune-safe chunk of ``payload``."""

    def rune_end(start: int) -> int:
        # advance past UTF-8 continuation bytes (10xxxxxx) to the next lead byte
        base: Final = start + chunk_size
        skip: Final = next(
            (k for k in range(_UTF8_MAX_CONTINUATION) if payload[base + k] & 0xC0 != 0x80),
            _UTF8_MAX_CONTINUATION,
        )
        return base + skip

    def bounds() -> Iterator[tuple[int, int]]:
        start = 0  # rebind-ok: loop cursor over the payload, advanced to each chunk end
        while start + chunk_size + _UTF8_MAX_CONTINUATION <= len(payload):
            end: Final = rune_end(start)
            yield (start, end)
            start = end  # rebind-ok: loop cursor over the payload, not accumulated state

    return tuple(bounds())


def _chunk_events(payload: bytes, chunk_size: int) -> Iterator[HashEvent]:
    """Rune-safe chunk events for a large payload, plus its end checkpoint."""
    bounds: Final = _chunk_bounds(payload, chunk_size)
    for start, end in bounds:
        yield (_FRAME_CHUNK, payload[start:end])
    consumed: Final = bounds[-1][1] if bounds else 0
    yield (_FRAME_END, _field(payload[consumed:]), len(payload).to_bytes(8, "little"))


def _frame_events(frame: Frame, chunk_size: int) -> Iterator[HashEvent]:
    """Events for one frame: start checkpoint, content chunks, end checkpoint."""
    frame_kind, role, payload = frame
    yield (_FRAME_START, _field(frame_kind.encode()), _field(role.encode()))
    if len(payload) > chunk_size:
        yield from _chunk_events(payload, chunk_size)
    else:
        yield (_FRAME_END, _field(payload), len(payload).to_bytes(8, "little"))


def build_chain(request: RequestView, model_group: str, cache_salt: str, chunk_size: int) -> tuple[bytes, ...]:
    """
    Running chain of digest nodes for one request.

    Deterministic for identical input. Returns raw digest bytes (the store
    hex-encodes at the Redis boundary). No node cap: the chain length is
    bounded by the request's own content.
    """
    from litellm.router_utils.session_identity.canonicalizer import canonical_frames

    frames: Final = canonical_frames(request)
    if not frames or chunk_size <= 0:
        return ()

    terminal: Final = (_TERMINAL, len(frames).to_bytes(4, "little"))
    # from_iterable flattens the per-frame event generators into one event
    # stream; chain then appends the single terminal event (kept as a 1-tuple so
    # it is yielded whole, not flattened).
    events: Final = _flatten(
        _flatten.from_iterable(_frame_events(frame, chunk_size) for frame in frames),
        (terminal,),
    )
    # accumulate yields the seed first; drop it and keep the per-event nodes.
    return tuple(accumulate(events, _fold, initial=root_seed(model_group, cache_salt)))[1:]


def declared_id(request: RequestView) -> str | None:
    """
    The client's own name for this session, from body fields that name a
    session across turns. ``previous_response_id`` is deliberately absent: it
    names the previous turn, not the session.

    Returns the key-namespaced id so "conversation" and "prompt_cache_key"
    values cannot alias each other, or None. Long values are hashed rather than
    truncated so two ids sharing a prefix do not collapse onto one lineage.
    """
    candidates: Final = (
        ("prompt_cache_key", request.get("prompt_cache_key")),
        ("conversation", request.get("conversation")),
    )
    return next(
        (
            f"{key}\x00{hashlib.sha256(value.encode()).hexdigest() if len(value) > _MAX_DECLARED_ID_LEN else value}"
            for key, value in candidates
            if isinstance(value, str) and value
        ),
        None,
    )


def scoped_declared_session_id(declared: str, model_group: str, scope: str) -> str:
    """Deterministic session id for a declared conversation name; no Redis needed."""
    return hashlib.sha256(
        b"litellm-session-declared-v1\x00"
        + scope.encode()
        + b"\x00"
        + model_group.encode()
        + b"\x00"
        + declared.encode()
    ).hexdigest()[:32]


def synthesized_session_id(chain: tuple[bytes, ...], model_group: str, scope: str) -> str:
    """Stable id for a fresh lineage, derived from its deepest node."""
    deepest: Final = chain[-1].hex() if chain else "empty"
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
