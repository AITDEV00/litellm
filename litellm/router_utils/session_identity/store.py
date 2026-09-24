"""Redis-backed lineage store.

Reads are Redis-authoritative when Redis is attached: the DualCache's own batch
path throttles repeated fetches of *missing* keys (~10s), which would let one
pod's stale "this hash does not exist" answer hide another pod's fresh teach.
Without Redis the DualCache degrades to per-pod in-memory. No local backfill:
with Redis attached every lookup goes to Redis, so a backfilled value is never read.

Lookups search the chain newest-to-oldest in ``_LOOKUP_BATCH`` windows, so a
normal continuation is one small MGET rather than a full-history one. Writes are
a single authoritative pipeline; teaching is best-effort, failures are logged.
"""

import hashlib
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Final

from litellm.constants import SESSION_IDENTITY_CACHE_KEY_PREFIX
from litellm.router_utils.session_identity.lineage import LineageMatch
from litellm.router_utils.session_identity.views import (
    LineageCacheReader,
    parse_lineage_record,
)

if TYPE_CHECKING:
    from litellm.caching.dual_cache import DualCache

verbose_logger: Final = logging.getLogger("litellm")

_DEFAULT_TTL: Final = 86_400  # 24h idle, aligned with deployment_affinity_ttl_seconds
_LOOKUP_BATCH: Final = 128  # reverse-search window


class SessionIdentityStore:
    def __init__(self, cache: "DualCache", ttl_seconds: int = _DEFAULT_TTL):
        self.cache: Final = cache
        # Both read AND write go to the concrete backend, never the DualCache:
        # DualCache.async_set_cache_pipeline catches backend exceptions and logs
        # them without re-raising, so a write through it cannot prove it
        # persisted. teach().persisted must be truthful, so writes bypass it.
        self._redis: Final = getattr(cache, "redis_cache", None)
        self._memory: Final = getattr(cache, "in_memory_cache", None)
        self.ttl_seconds = ttl_seconds

    def _key(self, node_hex: str, model_group: str, scope: str) -> str:
        # hash the model/scope namespace so key length is bounded and the
        # separator can never be ambiguous, regardless of what either contains.
        namespace: Final = hashlib.blake2b(
            model_group.encode() + b"\x00" + scope.encode(), digest_size=16, person=b"litellm-sid"
        ).hexdigest()
        return f"{SESSION_IDENTITY_CACHE_KEY_PREFIX}:{namespace}:{node_hex}"

    def _reader(self) -> LineageCacheReader | None:
        return self._redis if self._redis is not None else self._memory

    async def _write(self, cache_list: tuple[tuple[str, object], ...]) -> None:
        """Persist ``cache_list`` to the active backend, raising on failure.

        Redis: raw client pipeline, raising on any per-command error. In-memory:
        its own pipeline, which propagates. Either path surfaces failure, unlike
        the DualCache wrapper."""
        import json as _json

        if self._redis is not None:
            client: Final = self._redis.init_async_client()
            async with client.pipeline(transaction=False) as pipe:
                for key, value in cache_list:
                    pipe.set(name=key, value=_json.dumps(value), ex=self.ttl_seconds)
                results: Final = await pipe.execute()
            for r in results:
                if isinstance(r, Exception):
                    raise r
        elif self._memory is not None:
            await self._memory.async_set_cache_pipeline(cache_list=list(cache_list), ttl=self.ttl_seconds)
        else:
            raise RuntimeError("session_identity: no cache backend to teach to")

    async def lookup(self, chain: tuple[bytes, ...], model_group: str, scope: str) -> LineageMatch | None:
        reader: Final = self._reader()
        if reader is None or not chain:
            return None
        total: Final = len(chain)
        for hi in range(total, 0, -_LOOKUP_BATCH):
            lo = max(0, hi - _LOOKUP_BATCH)
            keys = [self._key(node.hex(), model_group, scope) for node in reversed(chain[lo:hi])]
            try:
                raw = await reader.async_batch_get_cache(keys)
            except Exception as e:  # noqa: BLE001  # fail-open: a cache miss routes normally
                verbose_logger.warning("session_identity: lineage lookup failed: %s", e)
                return None
            if not raw:
                continue
            # Redis returns a Mapping keyed by key; in-memory returns a positional list.
            values = [raw.get(k) for k in keys] if isinstance(raw, Mapping) else list(raw)
            for offset, value in enumerate(values):
                if value is None:
                    continue
                record = parse_lineage_record(value)
                if record is None:
                    continue
                matched = hi - offset  # offset 0 is the deepest node in this window
                return LineageMatch(
                    session_id=record["session_id"], matched_depth=matched, taught_chain_len=record["chain_len"]
                )
        return None

    async def teach(
        self, chain: tuple[bytes, ...], session_id: str, model_group: str, scope: str, start_index: int = 0
    ) -> bool:
        """Write chain nodes -> session. Returns True when persisted (or nothing
        to write), False when the backend write failed. Failures are logged, not
        raised: the caller decides whether an unpersisted lineage is fatal."""
        if not chain or not session_id:
            return True
        payload: Final = {"session_id": session_id, "chain_len": len(chain)}
        cache_list: Final = tuple((self._key(node.hex(), model_group, scope), payload) for node in chain[start_index:])
        if not cache_list:
            return True
        try:
            await self._write(cache_list)
            return True
        except Exception as e:  # noqa: BLE001  # fail-open: caller decides if an unpersisted lineage is fatal
            verbose_logger.warning("session_identity: lineage teach failed (%d keys): %s", len(cache_list), e)
            return False
