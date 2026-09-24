"""
Redis-backed lineage store for session-identity inference.

Clone of the PromptCachingCache wrapper pattern
(``litellm/router_utils/prompt_caching_cache.py``): hold the DualCache the
proxy passes to pre-call hooks, namespace keys, JSON values. In a proxy
deployment with ``enable_redis_auth_cache: true`` this cache is Redis-backed
(proxy_server.py attaches redis_usage_cache to user_api_key_cache), so lineage
state is shared across LiteLLM pods with no extra wiring. Without Redis the
DualCache degrades to per-pod in-memory, which is the correct fallback.
"""

import json
from typing import TYPE_CHECKING, Any, Final

from litellm.constants import SESSION_IDENTITY_CACHE_KEY_PREFIX

if TYPE_CHECKING:
    from litellm.caching.dual_cache import DualCache
else:
    from litellm.caching.dual_cache import DualCache

_DEFAULT_TTL: Final = 86_400  # 24h idle, aligned with deployment_affinity_ttl_seconds in prod


class SessionIdentityStore:
    def __init__(self, cache: DualCache, ttl_seconds: int = _DEFAULT_TTL):
        self.cache = cache
        self.ttl_seconds = ttl_seconds

    def _key(self, chain_hash: str, model_group: str, scope: str) -> str:
        return f"{SESSION_IDENTITY_CACHE_KEY_PREFIX}:{model_group}:{scope}:{chain_hash}"

    async def lookup(self, chain: list[str], model_group: str, scope: str) -> tuple[str, int] | None:
        """
        Deepest lineage match for ``chain``.

        Returns (session_id, matched_depth) for the deepest chain hash with a
        stored mapping, or None. Chain position i proves byte-prefix identity
        through chunk i, so the deepest hit is the lineage the request
        continues.

        Reads are Redis-authoritative when Redis is attached: the DualCache's
        own batch path throttles repeated fetches of *missing* keys
        (``redis_batch_cache_expiry``, ~10s), which would let one pod's stale
        "this hash does not exist" answer hide another pod's fresh teach for
        seconds. Lineage discovery needs the shared backend's answer now, so
        we read it directly and backfill positive hits into the local tier.
        """
        if not chain:
            return None
        keys = [self._key(h, model_group, scope) for h in reversed(chain)]
        results: list[Any] | None = None
        try:
            if self.cache.redis_cache is not None:
                redis_result = await self.cache.redis_cache.async_batch_get_cache(keys)
                if redis_result:
                    results = [redis_result.get(k) for k in keys]
                    # backfill positive hits so subsequent local reads are fast
                    if self.cache.in_memory_cache is not None:
                        for key, value in zip(keys, results):
                            if value is not None:
                                await self.cache.in_memory_cache.async_set_cache(key, value, ttl=self.ttl_seconds)
            elif self.cache.in_memory_cache is not None:
                results = await self.cache.in_memory_cache.async_batch_get_cache(keys)
        except Exception:
            return None
        if not results:
            return None
        for depth_from_end, value in enumerate(results):
            if value is None:
                continue
            session_id = self._session_id(value)
            if session_id is None:
                continue
            matched = len(chain) - depth_from_end
            return session_id, matched
        return None

    async def teach(
        self,
        chain: list[str],
        session_id: str,
        model_group: str,
        scope: str,
        start_index: int = 0,
    ) -> None:
        """
        Record chain hashes -> session mapping. Idempotent; refreshes TTL.

        ``start_index`` lets a growing conversation write only its NEW chain
        nodes: a match at depth d means nodes 0..d-1 are already recorded
        under this session id, so a normal turn appending one message writes
        O(new-turn-size) nodes instead of O(full-history-size).
        """
        if not chain or not session_id:
            return
        payload = {"session_id": session_id, "chain_len": len(chain)}
        cache_list = [
            (self._key(chain_hash, model_group, scope), payload)
            for chain_hash in chain[start_index:]
        ]
        if not cache_list:
            return
        try:
            await self.cache.async_set_cache_pipeline(cache_list=cache_list, ttl=self.ttl_seconds)
        except Exception:
            for key, value in cache_list:
                try:
                    await self.cache.async_set_cache(key, value, ttl=self.ttl_seconds)
                except Exception:
                    return

    @staticmethod
    def _session_id(value: Any) -> str | None:
        if isinstance(value, dict):
            sid = value.get("session_id")
            return str(sid) if sid else None
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return None
            if isinstance(parsed, dict):
                sid = parsed.get("session_id")
                return str(sid) if sid else None
        return None
