"""
Shared-prefix discrimination (SGLang RFC #34513 design; no upstream code exists).

Large shared prefixes - one 30k system prompt behind every Copilot/OpenCode
conversation - make the leading chain hashes identical across unrelated
sessions. A position many sessions have served is not evidence that two
requests belong to the same conversation, so the matcher must ignore it and
resolve on the distinguishing conversation suffix.

The tracker learns lazily from traffic: when teaching a lineage records a hash
that is already bound to a *different* session, that hash's common counter is
incremented. A hash whose counter reaches ``threshold`` (distinct sessions) is
treated as common and excluded from matching. Sessions whose entire chain is
common resolve to nothing, which is the correct behavior - without
distinguishing content there is no affinity signal.

State lives in the same shared cache as the lineage store so the learned
common set is consistent across LiteLLM pods.
"""

from typing import TYPE_CHECKING, Any, Final

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

    async def _get(self, key: str) -> dict[str, Any] | None:
        try:
            value = await self.store.cache.async_get_cache(key=key)
        except Exception:
            return None
        if isinstance(value, dict):
            return value
        return None

    async def observe(
        self,
        chain: list[str],
        model_group: str,
        scope: str,
        session_id: str,
    ) -> None:
        """
        Update common-hash counters while teaching a lineage.

        For each hash: read what sessions it is bound to; if the stored
        session differs from the one being taught, the hash is shared by more
        than one session, so bump its counter. Also rewrites the hash's
        session binding if absent (teach() writes it too; this keeps the
        counter read and the binding write adjacent).
        """
        for chain_hash in chain:
            key = self._key(chain_hash, model_group, scope)
            current = await self._get(key)
            if current is None:
                await self.store.cache.async_set_cache(
                    key, {"count": 1, "first_session": session_id}, ttl=_COMMON_TTL
                )
                continue
            if current.get("first_session") == session_id:
                continue
            count = int(current.get("count", 1)) + 1
            await self.store.cache.async_set_cache(
                key, {"count": count, "first_session": current.get("first_session")}, ttl=_COMMON_TTL
            )

    async def is_common(self, chain_hash: str, model_group: str, scope: str) -> bool:
        current = await self._get(self._key(chain_hash, model_group, scope))
        if current is None:
            return False
        return int(current.get("count", 0)) >= self.threshold

    async def filter_common(
        self,
        chain: list[str],
        model_group: str,
        scope: str,
    ) -> list[str]:
        """Drop chain hashes that many sessions share, keeping distinguishing ones."""
        result: list[str] = []
        for chain_hash in chain:
            if not await self.is_common(chain_hash, model_group, scope):
                result.append(chain_hash)
        return result
