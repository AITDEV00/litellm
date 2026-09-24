"""
Lineage resolution over the stored hash-chain index.

history_matcher ties the pieces together: canonicalize the request, build the
chain, filter out common (shared-prefix) hashes, then resolve the deepest
remaining lineage from the store. Teaching records the lineage after a
response, mirroring vLLM PR #217's session-map teaching but with Redis as the
shared map instead of a per-router DashMap.
"""

from typing import TYPE_CHECKING

from litellm.router_utils.session_identity.canonicalizer import canonicalize_chat
from litellm.router_utils.session_identity.hash_chain import (
    chain_continuation,
    chunk_chain,
    root_seed,
    synthesized_session_id,
)
from litellm.router_utils.session_identity.prefix_discriminator import (
    CommonPrefixTracker,
)
from litellm.router_utils.session_identity.store import SessionIdentityStore

if TYPE_CHECKING:
    from litellm.router_utils.session_identity.config import SessionIdentityConfig


class HistoryMatcher:
    def __init__(
        self,
        store: SessionIdentityStore,
        config: "SessionIdentityConfig",
        common_prefixes: CommonPrefixTracker | None = None,
    ):
        self.store = store
        self.config = config
        self.common_prefixes = common_prefixes or CommonPrefixTracker(
            store=store, threshold=config.common_prefix_threshold
        )

    def build_chain(self, data: dict, model_group: str, seed: bytes | None = None) -> list[str]:
        """Content chain for a request body. Deterministic for identical input."""
        if seed is None:
            seed = root_seed(model_group, self.config.cache_salt)
        stream = canonicalize_chat(data)
        return chunk_chain(stream, seed, self.config.chunk_size_bytes, self.config.max_chain_hashes)

    async def resolve(
        self,
        chain: list[str],
        model_group: str,
        scope: str,
    ) -> str | None:
        """
        Session id for a request whose chain hits stored lineage, or None.

        Common (shared-prefix) hashes are excluded before lookup: a position
        many sessions share is not a signal that two requests are the same
        conversation. If nothing distinguishing remains, the request has no
        usable lineage and routes normally.
        """
        if not chain:
            return None
        distinguishing = await self.common_prefixes.filter_common(
            chain=chain, model_group=model_group, scope=scope
        )
        if not distinguishing:
            return None
        match = await self.store.lookup(
            chain=distinguishing, model_group=model_group, scope=scope
        )
        if match is None:
            return None
        session_id, _matched_depth = match
        return session_id

    async def infer_session_id(
        self,
        data: dict,
        model_group: str,
        scope: str,
        declared: str | None = None,
    ) -> str | None:
        """
        Resolve-or-create the session id for a request.

        Order: stored lineage from distinguishing hashes, else a stable
        synthesized id when the chain itself is new but non-trivial (a fresh
        conversation gets an id immediately so its first taught lineage and the
        id used to pin it agree from turn one). Returns None only for
        requests without enough content to form a lineage.
        """
        chain = self.build_chain(data=data, model_group=model_group)
        if not chain:
            return None
        resolved = await self.resolve(chain=chain, model_group=model_group, scope=scope)
        if resolved is not None:
            return resolved
        return synthesized_session_id(chain=chain, model_group=model_group, scope=scope)

    async def teach(
        self,
        data: dict,
        model_group: str,
        scope: str,
        session_id: str,
        declared: str | None = None,
    ) -> None:
        """
        Record the request's chain under ``session_id`` after a response.

        Called from the success event, when the deployment that served the
        request is known. Idempotent per hash; a lineage that keeps being
        served keeps its TTL refreshed.
        """
        chain = self.build_chain(data=data, model_group=model_group)
        if not chain:
            return
        await self.common_prefixes.observe(
            chain=chain, model_group=model_group, scope=scope, session_id=session_id
        )
        await self.store.teach(
            chain=chain, session_id=session_id, model_group=model_group, scope=scope
        )

    async def extend_lineage(
        self,
        prior_chain: list[str],
        new_turn: dict,
        model_group: str,
        scope: str,
        session_id: str,
    ) -> list[str]:
        """
        Grow one remembered lineage with a turn that did not resend history.

        Not used by the Chat Completions path (clients resend full history);
        kept for server-side-history surfaces.
        """
        stream_new = canonicalize_chat(new_turn)
        extended = chain_continuation(
            stream=stream_new,
            prior_chain=prior_chain,
            seed=root_seed(model_group, self.config.cache_salt),
            chunk_size=self.config.chunk_size_bytes,
            max_chunks=self.config.max_chain_hashes,
        )
        await self.store.teach(
            chain=extended, session_id=session_id, model_group=model_group, scope=scope
        )
        return extended
