"""
Shared-prefix discrimination (SGLang RFC #34513 design; no upstream code exists).

Large shared prefixes - one 30k system prompt behind every Copilot/OpenCode
conversation - make the leading chain hashes identical across unrelated
sessions. A position many sessions have served is not evidence that two
requests belong to the same conversation, so the matcher must ignore it and
resolve on the distinguishing conversation suffix.

Distinct sessions are tracked exactly: each teaching SADDs the session id
into a candidate set (Redis SET on the shared backend; idempotent and
atomic). When the set's cardinality reaches ``threshold`` the hash is marked
common in a short-lived key and the candidate set is deleted, keeping hot
shared prefixes cheap on both the read and write path.

State lives in the same shared cache as the lineage store so the learned
common set is consistent across LiteLLM pods.
"""

from typing import TYPE_CHECKING, Final

from litellm.constants import (
    SESSION_IDENTITY_COMMON_CANDIDATES_PREFIX,
    SESSION_IDENTITY_COMMON_HASH_PREFIX,
)

if TYPE_CHECKING:
    from litellm.router_utils.session_identity.store import SessionIdentityStore

_DEFAULT_THRESHOLD: Final = 3  # distinct sessions sharing a hash before it is "common"
_COMMON_TTL: Final = 7 * 86_400  # a week: shared templates change slowly


class CommonPrefixTracker:
    def __init__(self, store: "SessionIdentityStore", threshold: int = _DEFAULT_THRESHOLD):
        self.store = store
        self.threshold = threshold

    def _key(self, chain_hash: str, model_group: str, scope: str) -> str:
        # Scoped by model_group but deliberately not by caller scope: the
        # shared prefix is shared across callers too.
        return f"{SESSION_IDENTITY_COMMON_HASH_PREFIX}:{model_group}:{chain_hash}"

    def _candidates_key(self, chain_hash: str, model_group: str, scope: str) -> str:
        return f"{SESSION_IDENTITY_COMMON_CANDIDATES_PREFIX}:{model_group}:{chain_hash}"

    async def is_common(self, chain_hash: str, model_group: str, scope: str) -> bool:
        try:
            marker = await self.store.cache.async_get_cache(key=self._key(chain_hash, model_group, scope))
        except Exception:
            return False
        return marker is not None

    async def observe(
        self,
        chain: list[str],
        model_group: str,
        scope: str,
        session_id: str,
    ) -> None:
        """
        Update distinct-session sets while teaching a lineage.

        SADD is idempotent, so re-teaching the same session id for a hash is
        a no-op; the set only grows when a genuinely different session is
        served under the same hash.
        """
        if not chain:
            return
        marker_keys = [self._key(h, model_group, scope) for h in chain]
        try:
            markers = await self.store.cache.async_batch_get_cache(keys=marker_keys)
        except Exception:
            markers = [None] * len(chain)
        for chain_hash, marker in zip(chain, markers):
            if marker is not None:
                continue  # already known common
            candidates_key = self._candidates_key(chain_hash, model_group, scope)
            try:
                await self.store.cache.async_set_cache_sadd(candidates_key, [session_id], ttl=_COMMON_TTL)
                members = await self.store.cache.async_get_cache(key=candidates_key)
            except Exception:
                continue
            size = len(members) if isinstance(members, (set, frozenset, list, tuple)) else 0
            if size >= self.threshold:
                try:
                    await self.store.cache.async_set_cache(
                        self._key(chain_hash, model_group, scope), 1, ttl=_COMMON_TTL
                    )
                    await self.store.cache.async_delete_cache(candidates_key)
                except Exception:
                    pass

    async def filter_common(
        self,
        chain: list[str],
        model_group: str,
        scope: str,
    ) -> list[str]:
        """Drop chain hashes that many sessions share, keeping distinguishing ones."""
        if not chain:
            return []
        keys = [self._key(h, model_group, scope) for h in chain]
        try:
            markers = await self.store.cache.async_batch_get_cache(keys=keys)
        except Exception:
            return list(chain)
        if not markers:
            return list(chain)
        return [h for h, marker in zip(chain, markers) if marker is None]
