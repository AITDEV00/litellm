"""
Content hashing for session-identity inference.

Port of the llm-d sessionprefixcache chunk.go / alias.go algorithm
(https://github.com/chethanuk/llm-d-router/pull/14, Apache-2.0), simplified to
the parts that apply to a Chat Completions gateway:

- A conversation is serialized to a byte stream (canonicalizer), then cut into
  complete, rune-safe chunks of at least ``chunk_size`` bytes. The trailing
  partial chunk is dropped: sub-chunk growth carries no reuse signal, matching
  how a model server's KV block only becomes reusable once a full block is
  filled.
- Every hash folds in the previous one, so a matching hash at position i proves
  the entire byte prefix through chunk i is identical. Passing a previous
  chain's last hash as the seed continues that chain.
- A chunk is emitted only when the bytes that decide its boundary are all
  present, so appending to a stream never moves an earlier boundary.

Only this service reads these keys, so the digest is stdlib sha256 rather than
xxhash; the chain shape (8-byte little-endian previous hash folded into each
digest) is preserved from the reference implementation.
"""

import hashlib
from typing import Final

_UTF8_MAX_CONTINUATION: Final = 3  # utf8.UTFMax - 1: most continuation bytes a boundary can skip


def root_seed(model_group: str, cache_salt: str) -> bytes:
    """
    Seed scoping a chain to a model group and optional cache salt.

    NUL-delimited so that ("ab", "c") and ("a", "bc") hash differently.
    """
    return hashlib.sha256(model_group.encode() + b"\x00" + cache_salt.encode()).digest()


def chunk_chain(
    stream: bytes,
    seed: bytes,
    chunk_size: int,
    max_chunks: int,
) -> list[str]:
    """
    Running chain of hex hashes, one per complete chunk of ``stream``.

    A chain is empty until ``stream`` holds at least one full chunk plus the
    UTF-8 reserve, so a first turn too small to fill a chunk produces no
    lineage by itself - it needs the distinguishing content of a second turn.
    """
    if chunk_size <= 0 or max_chunks <= 0:
        return []

    chain: list[str] = []
    prev = seed
    i = 0
    while i + chunk_size + _UTF8_MAX_CONTINUATION <= len(stream) and len(chain) < max_chunks:
        end = i + chunk_size
        # Extend past UTF-8 continuation bytes (10xxxxxx) to the next leading byte.
        while end < i + chunk_size + _UTF8_MAX_CONTINUATION and stream[end] & 0xC0 == 0x80:
            end += 1
        digest = hashlib.sha256()
        digest.update(prev)
        digest.update(stream[i:end])
        prev = digest.digest()
        chain.append(prev.hex())
        i = end
    return chain


def chain_continuation(
    stream: bytes,
    prior_chain: list[str],
    seed: bytes,
    chunk_size: int,
    max_chunks: int,
) -> list[str]:
    """
    Extend ``prior_chain`` with hashes of new ``stream`` content.

    Used when a caller supplies only the newest turn of a session whose earlier
    turns the gateway remembers. The prior chain's last hash becomes the seed,
    which is what keeps one growing lineage instead of starting a new chain.
    """
    if not prior_chain:
        return chunk_chain(stream, seed, chunk_size, max_chunks)
    remaining = max_chunks - len(prior_chain)
    if remaining <= 0:
        return list(prior_chain)
    continuation_seed = bytes.fromhex(prior_chain[-1])
    tail = chunk_chain(stream, continuation_seed, chunk_size, remaining)
    return prior_chain + tail


def shared_prefix_len(a: list[str], b: list[str]) -> int:
    """How many leading hashes ``a`` and ``b`` have in common."""
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    return n


def declared_id(data: dict) -> str | None:
    """
    The client's own name for this session, from body fields that name a
    session across turns (llm-d declaredIDKeys). ``previous_response_id`` is
    deliberately absent: it names the previous turn, not the session.

    The value never enters a chain hash; it is only a lookup key. Returns the
    key-namespaced id so "conversation" and "prompt_cache_key" values cannot
    alias each other, or None when the client declared nothing. Long values
    are hashed rather than truncated (llm-d clampID): truncation would collapse
    two sessions whose ids share a prefix onto one lineage.
    """
    max_declared_id_len: Final = 256
    for key in ("prompt_cache_key", "conversation"):
        value = data.get(key)
        if not isinstance(value, str) or not value:
            continue
        if len(value) > max_declared_id_len:
            value = hashlib.sha256(value.encode()).hexdigest()
        return f"{key}\x00{value}"
    return None


def synthesized_session_id(chain: list[str], model_group: str, scope: str) -> str:
    """
    Stable id for a matched lineage, derived from its deepest hash.

    Deterministic per lineage: the same conversation re-resolves to the same
    id across requests and LiteLLM pods without storing a separate id mapping.
    """
    deepest = chain[-1] if chain else "empty"
    return hashlib.sha256(
        b"session_identity\x00" + deepest.encode() + b"\x00" + model_group.encode() + b"\x00" + scope.encode()
    ).hexdigest()[:32]
