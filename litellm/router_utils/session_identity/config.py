"""
Environment-tunable knobs for session-identity inference.

All optional; defaults match the implementation plan doc
(``docs/session-identity/IMPLEMENTATION-PLAN.md``). Backed by pydantic-settings
(a prod dependency), so values parse/validate at callback construction with a
clear error on a bad env var instead of silently falling back.

TTL must outlive the deployment-affinity pin (86400s in the prod/dev yaml):
a shorter lineage TTL makes the resolver forget "history -> session id" while
the pin still exists, and the resumed conversation gets a NEW id while the old
pin is unreachable.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from litellm.constants import SESSION_IDENTITY_DEFAULT_TTL_SECONDS


class SessionIdentityConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SESSION_IDENTITY_", frozen=True)

    enabled: bool = False
    chunk_size_bytes: int = Field(default=2048, ge=256, le=16384)
    max_chain_hashes: int = Field(default=256, ge=8)
    ttl_seconds: int = Field(default=SESSION_IDENTITY_DEFAULT_TTL_SECONDS, ge=60)
    common_prefix_threshold: int = Field(default=3, ge=2)
    cache_salt: str = ""

    @classmethod
    def from_env(cls) -> "SessionIdentityConfig":
        return cls()
