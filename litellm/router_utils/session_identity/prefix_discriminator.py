"""
Shared-prefix discrimination (SGLang RFC #34513 design; no upstream code exists).

Large shared prefixes - one 30k system prompt behind every Copilot/OpenCode
conversation - make the leading chain hashes identical across unrelated
sessions. A position many sessions have served is not evidence that two
requests belong to the same conversation, so the matcher must ignore it and
resolve on the distinguishing conversation suffix.

The tracker learns lazily from traffic: each hash's counter is incremented
atomically via ``DualCache.async_increment_cache`` (a Redis INCR on the shared
backend) whenever a lineage teach binds that hash to a session other than the
one first bound to it. A hash whose counter reaches ``threshold`` (distinct
sessions) is treated as common and excluded from matching. Sessions whose
entire chain is common resolve to nothing, which is the correct behavior -
without distinguishing content there is no affinity signal.

State lives in the same shared cache as the lineage store so the learned
common set is consistent across LiteLLM pods.
"""

from typing import TYPE_CHECKING, Final

from litellm.constants import SESSION_IDENTITY_COMMON_HASH_PREFIX

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

    def _first_key(self, chain_hash: str, model_group: str, scope: str) -> str:
        """Which session first bound this hash (used to dedupe counter increments)."""
        return f"{self._key(chain_hash, model_group, scope)}:first"

    async def _get(self, key: str) -> int | None:
        try:
            value = await self.store.cache.async_get_cache(key=key)
        except Exception:
            return None
        return int(value) if isinstance(value, (int, float)) else None

    async def observe(
        self,
        chain: list[str],
        model_group: str,
        scope: str,
        session_id: str,
    ) -> None:
        """
        Update common-hash counters while teaching a lineage.

        One batched read of all first-session bindings, then for each hash
        either record this session as first (counter 1) or atomically INCR the
        counter when the first session differs. A re-teach of the same session
        is a no-op.
        """
        if not chain:
            return
        first_keys = [self._first_key(h, model_group, scope) for h in chain]
        try:
            firsts = await self.store.cache.async_batch_get_cache(keys=first_keys)
        except Exception:
            return
        if not firsts:
            return
        for chain_hash, first in zip(chain, firsts):
            count_key = self._key(chain_hash, model_group, scope)
            first_key = self._first_key(chain_hash, model_group, scope)
            if first is None:
                await self.store.cache.async_set_cache(first_key, session_id, ttl=_COMMON_TTL)
                await self.store.cache.async_set_cache(count_key, 1, ttl=_COMMON_TTL)
                continue
            if str(first) == session_id:
                continue
            await self.store.cache.async_increment_cache(count_key, 1, ttl=_COMMON_TTL)

    async def is_common(self, chain_hash: str, model_group: str, scope: str) -> bool:
        count = await self._get(self._key(chain_hash, model_group, scope))
        return count is not None and count >= self.threshold

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
            counts = await self.store.cache.async_batch_get_cache(keys=keys)
        except Exception:
            return list(chain)
        if not counts:
            return list(chain)
        return [
            h
            for h, count in zip(chain, counts)
            if not (isinstance(count, (int, float)) and int(count) >= self.threshold)
        ]
