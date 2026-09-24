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
        continues; the common-prefix discriminator decides whether that hit is
        a real signal before this is trusted (see prefix_discriminator).
        """
        if not chain:
            return None
        keys = [self._key(h, model_group, scope) for h in reversed(chain)]
        try:
            results = await self.cache.async_batch_get_cache(keys=keys)
        except Exception:
            results = None
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

    async def teach(self, chain: list[str], session_id: str, model_group: str, scope: str) -> None:
        """Record every chain hash -> session mapping. Idempotent; refreshes TTL."""
        if not chain or not session_id:
            return
        payload = {"session_id": session_id, "chain_len": len(chain)}
        cache_list = [
            (self._key(chain_hash, model_group, scope), payload) for chain_hash in chain
        ]
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
