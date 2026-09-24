"""
Redis-backed lineage store for session-identity inference.

Holds the DualCache the proxy passes to pre-call hooks, namespaces keys, and
stores ``{session_id, chain_len}`` per chain node. In a proxy deployment with
``enable_redis_auth_cache: true`` this cache is Redis-backed, so lineage state
is shared across LiteLLM pods with no extra wiring; without Redis the DualCache
degrades to per-pod in-memory, which is the correct fallback.

Reads are Redis-authoritative when Redis is attached: the DualCache's own batch
path throttles repeated fetches of *missing* keys (~10s), which would let one
pod's stale "this hash does not exist" answer hide another pod's fresh teach.
Lineage discovery needs the shared backend's answer now, so we read it directly
and backfill positive hits into the local tier. Misses are never cached locally.

Writes are a single authoritative pipeline. Teaching is best-effort: if the
pipeline fails we log and move on rather than run hundreds of sequential
retries during a Redis incident (the DualCache pipeline already swallows and
logs its own errors, so a per-key fallback would rarely even fire).
"""

import json
import logging
from typing import Any, Final

from litellm.constants import SESSION_IDENTITY_CACHE_KEY_PREFIX
from litellm.router_utils.session_identity.lineage import LineageMatch

verbose_logger = logging.getLogger("litellm")

_DEFAULT_TTL: Final = 86_400  # 24h idle, aligned with deployment_affinity_ttl_seconds in prod


class SessionIdentityStore:
    def __init__(self, cache: Any, ttl_seconds: int = _DEFAULT_TTL):
        self.cache = cache
        self.ttl_seconds = ttl_seconds

    def _key(self, node_hex: str, model_group: str, scope: str) -> str:
        return f"{SESSION_IDENTITY_CACHE_KEY_PREFIX}:{model_group}:{scope}:{node_hex}"

    async def lookup(self, chain: list[bytes], model_group: str, scope: str) -> LineageMatch | None:
        """
        Deepest lineage match for ``chain``, or None.

        Chain position i proves byte-prefix identity through node i, so the
        deepest hit is the lineage the request continues. The stored
        ``chain_len`` is returned alongside so the caller can classify
        continuation vs fork.
        """
        if not chain:
            return None
        keys = [self._key(node.hex(), model_group, scope) for node in reversed(chain)]
        results: list[Any] | None = None
        try:
            redis_cache = getattr(self.cache, "redis_cache", None)
            in_memory = getattr(self.cache, "in_memory_cache", None)
            if redis_cache is not None:
                redis_result = await redis_cache.async_batch_get_cache(keys)
                if redis_result:
                    results = [redis_result.get(k) for k in keys]
                    if in_memory is not None:
                        for key, value in zip(keys, results):
                            if value is not None:
                                await in_memory.async_set_cache(key, value, ttl=self.ttl_seconds)
            elif in_memory is not None:
                results = await in_memory.async_batch_get_cache(keys)
        except Exception as e:
            verbose_logger.warning("session_identity: lineage lookup failed: %s", e)
            return None
        if not results:
            return None
        for depth_from_end, value in enumerate(results):
            if value is None:
                continue
            parsed = self._parse(value)
            if parsed is None:
                continue
            session_id, taught_chain_len = parsed
            matched = len(chain) - depth_from_end
            return LineageMatch(session_id=session_id, matched_depth=matched, taught_chain_len=taught_chain_len)
        return None

    async def teach(self, chain: list[bytes], session_id: str, model_group: str, scope: str, start_index: int = 0) -> None:
        """
        Record chain nodes -> session mapping. Idempotent; refreshes TTL.

        ``start_index`` lets a growing conversation write only its NEW nodes:
        a match at depth d means nodes 0..d-1 are already recorded under this
        session id, so a normal turn appending one message writes O(new-turn)
        nodes instead of O(full-history). One pipeline; failures are logged and
        dropped (best-effort).
        """
        if not chain or not session_id:
            return
        payload = {"session_id": session_id, "chain_len": len(chain)}
        cache_list = [
            (self._key(node.hex(), model_group, scope), payload) for node in chain[start_index:]
        ]
        if not cache_list:
            return
        try:
            await self.cache.async_set_cache_pipeline(cache_list=cache_list, ttl=self.ttl_seconds)
        except Exception as e:
            verbose_logger.warning("session_identity: lineage teach failed (%d keys): %s", len(cache_list), e)

    @staticmethod
    def _parse(value: Any) -> tuple[str, int] | None:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return None
        if not isinstance(value, dict):
            return None
        sid = value.get("session_id")
        chain_len = value.get("chain_len")
        if not sid or not isinstance(chain_len, int):
            return None
        return str(sid), chain_len
