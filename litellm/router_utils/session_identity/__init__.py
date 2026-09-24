"""
Session-identity inference for deployment affinity.

Resolves or infers a stable per-conversation session id for requests that carry
none, so the existing ``DeploymentAffinityCheck`` session-affinity pin can keep
a conversation on the replica that holds its vLLM prefix KV cache.

Public integration surface only: wire ``SessionIdentityResolver`` into
``litellm_settings.callbacks`` and tune with ``SessionIdentityConfig``. The
internal modules (lineage, canonicalizer, store, views) are implementation
details, not supported API.
"""

from litellm.router_utils.session_identity.config import SessionIdentityConfig
from litellm.router_utils.session_identity.resolver import SessionIdentityResolver

__all__ = (
    "SessionIdentityConfig",
    "SessionIdentityResolver",
)
