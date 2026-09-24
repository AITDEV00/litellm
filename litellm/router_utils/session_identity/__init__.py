"""
Session-identity inference for deployment affinity.

Resolves or infers a stable per-conversation session id for requests that carry
none, so the existing ``DeploymentAffinityCheck`` session-affinity pin can keep
a conversation on the replica that holds its vLLM prefix KV cache. See
``docs/session-identity/IMPLEMENTATION-PLAN.md`` and the module docstrings.
"""

from litellm.router_utils.session_identity.config import SessionIdentityConfig
from litellm.router_utils.session_identity.lineage import (
    IdentityResolution,
    LineageMatch,
    build_chain,
    declared_id,
    root_seed,
    scoped_declared_session_id,
    synthesized_session_id,
)
from litellm.router_utils.session_identity.resolver import SessionIdentityResolver
from litellm.router_utils.session_identity.store import SessionIdentityStore

__all__ = [
    "IdentityResolution",
    "LineageMatch",
    "SessionIdentityConfig",
    "SessionIdentityResolver",
    "SessionIdentityStore",
    "build_chain",
    "declared_id",
    "root_seed",
    "scoped_declared_session_id",
    "synthesized_session_id",
]
