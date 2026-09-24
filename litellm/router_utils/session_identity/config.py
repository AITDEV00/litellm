"""
Environment-tunable knobs for session-identity inference.

All optional; defaults match the implementation plan doc
(``docs/session-identity/IMPLEMENTATION-PLAN.md``). Read once at callback
construction, not per request.
"""

import os
from dataclasses import dataclass

from litellm.constants import SESSION_IDENTITY_DEFAULT_TTL_SECONDS


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class SessionIdentityConfig:
    enabled: bool
    chunk_size_bytes: int
    max_chain_hashes: int
    ttl_seconds: int
    common_prefix_threshold: int
    cache_salt: str

    @classmethod
    def from_env(cls) -> "SessionIdentityConfig":
        return cls(
            enabled=os.getenv("SESSION_IDENTITY_ENABLED", "false").strip().lower() in ("true", "1", "yes"),
            chunk_size_bytes=_env_int("SESSION_IDENTITY_CHUNK_SIZE_BYTES", 512),
            max_chain_hashes=_env_int("SESSION_IDENTITY_MAX_CHAIN_HASHES", 64),
            ttl_seconds=_env_int("SESSION_IDENTITY_TTL_SECONDS", SESSION_IDENTITY_DEFAULT_TTL_SECONDS),
            common_prefix_threshold=_env_int("SESSION_IDENTITY_COMMON_PREFIX_THRESHOLD", 3),
            cache_salt=os.getenv("SESSION_IDENTITY_CACHE_SALT", ""),
        )
