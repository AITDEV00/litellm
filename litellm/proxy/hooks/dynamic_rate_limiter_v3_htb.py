"""
Dynamic rate limiter v3 - HTB (Hierarchical Token Bucket) priority-based rate limiting.

Custom fork variant of ``dynamic_rate_limiter_v3`` that adds hierarchical
token-bucket borrowing on top of the v3 limiter infrastructure. Kept in its own
module so upstream's ``dynamic_rate_limiter_v3.py`` and
``parallel_request_limiter_v3.py`` stay untouched and conflict-free on future
merges.

To enable, set the callback to ``dynamic_rate_limiter_v3_htb``.
"""

import asyncio
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime
from typing import TYPE_CHECKING, Any, Union

import httpx

import litellm
from litellm import ModelResponse, Router
from litellm._logging import verbose_proxy_logger
from litellm.caching.caching import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.hooks.parallel_request_limiter_v3 import (
    RateLimitDescriptor,
    RateLimitDescriptorRateLimitObject,
    RateLimitResponse,
    RateLimitStatus,
    _PROXY_MaxParallelRequestsHandler_v3,
)
from litellm.proxy.hooks.rate_limiter_utils import (
    convert_priority_to_percent,
    resolve_llm_provider_for_rate_limit,
)
from litellm.types.router import ModelGroupInfo
from litellm.types.utils import CallTypesLiteral

if TYPE_CHECKING:
    from opentelemetry.trace import Span as _Span

    Span = _Span | object
else:
    Span = object

htb_priority: ContextVar[str | None] = ContextVar("htb_priority", default=None)

if TYPE_CHECKING:
    from litellm.proxy.utils import InternalUsageCache
    from litellm.types.utils import PriorityReservationSettings


def _get_priority_settings() -> "PriorityReservationSettings":
    """
    Get the priority reservation settings, guaranteed to be non-None.

    The settings are lazy-loaded in litellm.__init__ and always return an instance.
    This helper provides proper type narrowing for mypy.
    """
    settings = litellm.priority_reservation_settings
    if settings is None:
        from litellm.types.utils import PriorityReservationSettings

        return PriorityReservationSettings()
    return settings


HTB_CHECK_AND_INCREMENT_SCRIPT = """
-- HTB (Hierarchical Token Bucket) check-and-increment with demand-based borrowing.
--
-- A demand counter (sliding-window, same as the request counter) is incremented
-- BEFORE this script runs. The demand counter reflects how many requests a
-- priority has ATTEMPTED in the current window, including requests that were
-- denied. This makes a priority's demand visible to siblings immediately,
-- even before its first request is processed by this script.
--
-- For each sibling, reservation = min(sibling_demand, sibling_guaranteed).
-- This means:
--   - Sibling with 200 demand (guaranteed=54): reservation = 54, fully protected
--   - Sibling with 10 demand  (guaranteed=54): reservation = 10, 44 borrowable
--   - Sibling with 0 demand   (guaranteed=54): reservation = 0,  fully borrowable
--   - Sibling idle (demand expired):           reservation = 0,  fully borrowable
--
-- Semantics:
--   1. Within guaranteed rate (priority_current < priority_limit):
--      ALLOW if model_current < model_limit.
--   2. Borrowing (priority_current >= priority_limit):
--      ALLOW if priority_current < borrow_ceiling AND model_current < model_limit.
--      borrow_ceiling = min(saturation_cap, model_limit) - sum_of_sibling_reservations
--   3. Otherwise: DENY.
--
-- KEYS layout (6 + 2*num_siblings keys):
--   KEYS[1] = priority window key
--   KEYS[2] = priority counter key (accepted requests)
--   KEYS[3] = model window key
--   KEYS[4] = model counter key (accepted requests)
--   KEYS[5] = (unused, reserved for backward compat)
--   KEYS[6] = (unused, reserved for backward compat)
--   KEYS[7..] = per sibling: (demand_window_key, demand_counter_key)
--
-- ARGV layout:
--   ARGV[1] = priority_limit        (guaranteed rate for this priority)
--   ARGV[2] = model_limit           (total model RPM)
--   ARGV[3] = ttl_seconds           (counter TTL when window resets)
--   ARGV[4] = window_size           (sliding-window length in seconds)
--   ARGV[5] = num_siblings          (number of sibling priority entries)
--   ARGV[6] = saturation_cap        (model_limit * saturation_threshold)
--   ARGV[7] = (unused, reserved for backward compat)
--   ARGV[8..] = sibling_guaranteed_rates (one per sibling)
--
-- Return:
--   { 0, priority_counter, model_counter, borrowed_flag }
--   { 1, current_priority_counter, priority_limit, 0 }
local time_reply = redis.call('TIME')
local now = tonumber(time_reply[1])

local priority_window = KEYS[1]
local priority_counter_key = KEYS[2]
local model_window = KEYS[3]
local model_counter_key = KEYS[4]

local priority_limit = tonumber(ARGV[1])
local model_limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
local window_size = tonumber(ARGV[4])
local num_siblings = tonumber(ARGV[5])
local saturation_cap = tonumber(ARGV[6])

-- Helper: read counter with window expiry
local function read_counter(window_key, counter_key)
    local window_start = redis.call('GET', window_key)
    local window_expired = (not window_start) or
        ((now - tonumber(window_start)) >= window_size)
    if window_expired then
        return 0, true
    else
        return tonumber(redis.call('GET', counter_key) or 0), false
    end
end

-- Helper: increment counter (reset if window expired)
local function increment_counter(window_key, counter_key, window_expired, ttl, window_size)
    if window_expired then
        redis.call('SET', window_key, tostring(now))
        redis.call('SET', counter_key, 1)
        redis.call('EXPIRE', window_key, window_size)
        if ttl > 0 then
            redis.call('EXPIRE', counter_key, ttl)
        end
        return 1
    else
        local new_counter = redis.call('INCRBY', counter_key, 1)
        local current_ttl = redis.call('TTL', counter_key)
        if current_ttl == -1 and ttl > 0 then
            redis.call('EXPIRE', counter_key, ttl)
        end
        return new_counter
    end
end

local priority_current, priority_window_expired = read_counter(priority_window, priority_counter_key)
local model_current, model_window_expired = read_counter(model_window, model_counter_key)

-- Demand-based borrow ceiling.
-- For each sibling, read their demand counter and reserve
-- min(demand, sibling_guaranteed).
local borrow_ceiling = math.min(saturation_cap, model_limit)
local arg_idx = 8
local key_idx = 7
for i = 1, num_siblings do
    local sib_demand_window_key = KEYS[key_idx]
    local sib_demand_counter_key = KEYS[key_idx + 1]
    local sibling_guaranteed = tonumber(ARGV[arg_idx])
    local sib_demand = read_counter(sib_demand_window_key, sib_demand_counter_key)
    local reservation = math.min(sib_demand, sibling_guaranteed)
    borrow_ceiling = borrow_ceiling - reservation
    arg_idx = arg_idx + 1
    key_idx = key_idx + 2
end
if borrow_ceiling < priority_limit then
    borrow_ceiling = priority_limit
end

-- DENY checks:
-- 1. Borrowing and priority has exceeded its borrow ceiling
-- 2. Model is at total capacity (hard limit, cannot be exceeded)
if priority_current >= priority_limit and priority_current >= borrow_ceiling then
    return { 1, priority_current, priority_limit, 0 }
end
if model_current >= model_limit then
    return { 1, priority_current, priority_limit, 0 }
end

local borrowed = 0
if priority_current >= priority_limit then
    borrowed = 1
end

local new_priority = increment_counter(priority_window, priority_counter_key, priority_window_expired, ttl, window_size)
local new_model = increment_counter(model_window, model_counter_key, model_window_expired, ttl, window_size)

return { 0, new_priority, new_model, borrowed }
"""


class _PROXY_HtbMaxParallelRequestsHandlerV3(_PROXY_MaxParallelRequestsHandler_v3):
    """
    v3 limiter extended with HTB (Hierarchical Token Bucket) check-and-increment.

    Subclasses the upstream v3 limiter so it stays untouched; this subclass
    only registers the extra HTB Lua script and adds the HTB enforcement
    methods on top.
    """

    def __init__(
        self,
        internal_usage_cache: Any,
        time_provider: Callable[[], datetime] | None = None,
    ):
        super().__init__(internal_usage_cache, time_provider=time_provider)
        if self.internal_usage_cache.dual_cache.redis_cache is not None:
            self.htb_check_and_increment_script = (
                self.internal_usage_cache.dual_cache.redis_cache.async_register_script(HTB_CHECK_AND_INCREMENT_SCRIPT)
            )
        else:
            self.htb_check_and_increment_script = None

    async def htb_check_and_increment(
        self,
        priority_descriptor: RateLimitDescriptor,
        model_descriptor: RateLimitDescriptor,
        parent_otel_span: Span | None = None,
        sibling_priorities: list[tuple[str, int]] | None = None,
        saturation_threshold: float = 1.0,
    ) -> RateLimitResponse:
        """
        HTB (Hierarchical Token Bucket) atomic check-and-increment.

        Checks a priority bucket (guaranteed rate) and a model-wide bucket
        (total capacity) atomically. If the priority bucket is within its
        guaranteed rate, the request is allowed. If the priority bucket
        exceeds its guaranteed rate but the model-wide bucket has spare
        capacity (accounting for other priorities' reservations), the
        request is allowed (borrowing). Otherwise, the request is denied.

        Uses a single Lua script for Redis (atomic across both buckets).
        Falls back to in-memory with an asyncio lock when Redis is unavailable.
        """
        p_rate_limit: RateLimitDescriptorRateLimitObject = (
            priority_descriptor.get("rate_limit") or RateLimitDescriptorRateLimitObject()
        )
        m_rate_limit: RateLimitDescriptorRateLimitObject = (
            model_descriptor.get("rate_limit") or RateLimitDescriptorRateLimitObject()
        )
        priority_limit = p_rate_limit.get("requests_per_unit")
        model_limit = m_rate_limit.get("requests_per_unit")
        if priority_limit is None or model_limit is None:
            return RateLimitResponse(overall_code="OK", statuses=[])

        window_size = int(p_rate_limit.get("window_size") or self.window_size)
        ttl = window_size

        htb_hash = f"htb:{model_descriptor['value']}"
        priority_suffix = priority_descriptor["value"]
        priority_window_key = f"{{{htb_hash}}}:{priority_suffix}:window"
        priority_counter_key = f"{{{htb_hash}}}:{priority_suffix}:requests"
        model_window_key = f"{{{htb_hash}}}:window"
        model_counter_key = f"{{{htb_hash}}}:requests"

        # Demand counter: incremented BEFORE the Lua script so siblings
        # can see this priority's demand even if its request hasn't been
        # processed yet. Uses the same sliding-window mechanism as the
        # request counter.
        my_demand_window_key = f"{{{htb_hash}}}:{priority_suffix}:demand:window"
        my_demand_counter_key = f"{{{htb_hash}}}:{priority_suffix}:demand:requests"

        saturation_cap = int(model_limit * saturation_threshold)

        # Build sibling demand keys (window + counter pairs) and guaranteed rates.
        sibling_keys: list[str] = []
        sibling_args: list[int] = []
        if sibling_priorities:
            for sibling_key, sibling_limit in sibling_priorities:
                sib_demand_window_key = f"{{{htb_hash}}}:{sibling_key}:demand:window"
                sib_demand_counter_key = f"{{{htb_hash}}}:{sibling_key}:demand:requests"
                sibling_keys.append(sib_demand_window_key)
                sibling_keys.append(sib_demand_counter_key)
                sibling_args.append(int(sibling_limit))

        # KEYS[5] and KEYS[6] are unused (reserved for backward compat with
        # the old EWMA layout so the KEYS index numbering stays stable).
        keys = [
            priority_window_key,
            priority_counter_key,
            model_window_key,
            model_counter_key,
            my_demand_window_key,
            my_demand_counter_key,
        ] + sibling_keys
        args = [
            int(priority_limit),
            int(model_limit),
            ttl,
            window_size,
            len(sibling_priorities or []),
            saturation_cap,
            0,
        ] + sibling_args

        # Increment demand counter BEFORE the lock/Lua script so siblings
        # can see this priority's demand even if its request hasn't been
        # processed yet. This is best-effort and non-atomic; a race only
        # means a sibling sees a slightly stale demand count.
        await self._increment_demand_counter(
            my_demand_window_key,
            my_demand_counter_key,
            window_size,
            ttl,
            parent_otel_span,
        )

        if self.htb_check_and_increment_script is not None:
            try:
                raw = await self.htb_check_and_increment_script(keys=keys, args=args)
                return self._build_htb_response(raw, priority_limit, model_limit)
            except Exception as e:  # noqa: BLE001 - any Redis/Lua failure degrades to in-memory HTB, never a 500
                verbose_proxy_logger.error(
                    f"htb_check_and_increment: Redis Lua failed ({type(e).__name__}: {e}). Falling back to in-memory."
                )

        async with self._check_and_increment_lock:
            return await self._htb_in_memory(
                priority_window_key=priority_window_key,
                priority_counter_key=priority_counter_key,
                model_window_key=model_window_key,
                model_counter_key=model_counter_key,
                priority_limit=int(priority_limit),
                model_limit=int(model_limit),
                ttl=ttl,
                window_size=window_size,
                parent_otel_span=parent_otel_span,
                sibling_priorities=sibling_priorities,
                saturation_threshold=saturation_threshold,
                htb_hash=htb_hash,
                priority_suffix=priority_suffix,
            )

    async def _increment_demand_counter(
        self,
        demand_window_key: str,
        demand_counter_key: str,
        window_size: int,
        ttl: int,
        parent_otel_span: Span | None = None,
    ) -> None:
        """Increment the demand counter before the Lua script runs.

        Writes to both in-memory and Redis (local_only=False) so siblings on
        other pods can read the demand via the Lua script's
        redis.call('GET', ...). When the window is active, uses an atomic
        Redis INCR (async_increment_cache) to eliminate the read-modify-write
        race across pods.
        """
        now = int(self._get_current_time().timestamp())
        window_start = await self.internal_usage_cache.async_get_cache(
            key=demand_window_key,
            litellm_parent_otel_span=parent_otel_span,
            local_only=False,
        )
        if window_start is None or (now - int(window_start)) >= window_size:
            await self.internal_usage_cache.async_set_cache(
                key=demand_window_key,
                value=now,
                ttl=window_size,
                litellm_parent_otel_span=parent_otel_span,
                local_only=False,
            )
            await self.internal_usage_cache.async_set_cache(
                key=demand_counter_key,
                value=1,
                ttl=ttl,
                litellm_parent_otel_span=parent_otel_span,
                local_only=False,
            )
        else:
            await self.internal_usage_cache.async_increment_cache(
                key=demand_counter_key,
                value=1,
                litellm_parent_otel_span=parent_otel_span,
                local_only=False,
                ttl=ttl,
            )
        await asyncio.sleep(0)

    def _build_htb_response(
        self,
        raw: list[Any],
        priority_limit: int,
        model_limit: int,
    ) -> RateLimitResponse:
        """Convert HTB Lua script return to RateLimitResponse."""
        if not raw:
            return RateLimitResponse(overall_code="OK", statuses=[])

        status_code = int(raw[0])
        if status_code == 1:
            return RateLimitResponse(
                overall_code="OVER_LIMIT",
                statuses=[
                    RateLimitStatus(
                        code="OVER_LIMIT",
                        current_limit=int(priority_limit),
                        limit_remaining=0,
                        rate_limit_type="requests",
                        descriptor_key="priority_model",
                    )
                ],
            )

        priority_counter = int(raw[1])
        model_counter = int(raw[2])
        return RateLimitResponse(
            overall_code="OK",
            statuses=[
                RateLimitStatus(
                    code="OK",
                    current_limit=int(priority_limit),
                    limit_remaining=max(0, int(priority_limit) - priority_counter),
                    rate_limit_type="requests",
                    descriptor_key="priority_model",
                ),
                RateLimitStatus(
                    code="OK",
                    current_limit=int(model_limit),
                    limit_remaining=max(0, int(model_limit) - model_counter),
                    rate_limit_type="requests",
                    descriptor_key="model_saturation_check",
                ),
            ],
        )

    async def _htb_in_memory(
        self,
        priority_window_key: str,
        priority_counter_key: str,
        model_window_key: str,
        model_counter_key: str,
        priority_limit: int,
        model_limit: int,
        ttl: int,
        window_size: int,
        parent_otel_span: Span | None = None,
        sibling_priorities: list[tuple[str, int]] | None = None,
        saturation_threshold: float = 1.0,
        htb_hash: str = "",
        priority_suffix: str = "",
    ) -> RateLimitResponse:
        """In-memory HTB fallback. Caller holds the lock.

        Demand counter was already incremented before the lock was acquired.
        """
        now_int = int(self._get_current_time().timestamp())

        async def _read(window_key: str, counter_key: str) -> tuple[int, bool]:
            window_start = await self.internal_usage_cache.async_get_cache(
                key=window_key,
                litellm_parent_otel_span=parent_otel_span,
                local_only=True,
            )
            window_expired = window_start is None or (now_int - int(window_start)) >= window_size
            if window_expired:
                return 0, True
            val = await self.internal_usage_cache.async_get_cache(
                key=counter_key,
                litellm_parent_otel_span=parent_otel_span,
                local_only=True,
            )
            return (int(val) if val is not None else 0), False

        async def _increment(window_key: str, counter_key: str, window_expired: bool) -> int:
            if window_expired:
                await self.internal_usage_cache.async_set_cache(
                    key=window_key,
                    value=now_int,
                    ttl=window_size,
                    litellm_parent_otel_span=parent_otel_span,
                    local_only=True,
                )
                new_val = 1
            else:
                current = await self.internal_usage_cache.async_get_cache(
                    key=counter_key,
                    litellm_parent_otel_span=parent_otel_span,
                    local_only=True,
                )
                new_val = (int(current) if current is not None else 0) + 1
            await self.internal_usage_cache.async_set_cache(
                key=counter_key,
                value=new_val,
                ttl=ttl,
                litellm_parent_otel_span=parent_otel_span,
                local_only=True,
            )
            return new_val

        priority_current, priority_expired = await _read(priority_window_key, priority_counter_key)
        model_current, model_expired = await _read(model_window_key, model_counter_key)

        # Demand-based borrow ceiling.
        # For each sibling, read their demand counter and reserve
        # min(demand, sibling_guaranteed).
        saturation_cap = int(model_limit * saturation_threshold)
        borrow_ceiling = min(saturation_cap, model_limit)
        if sibling_priorities:
            for sibling_key, sibling_limit in sibling_priorities:
                sib_demand_window_k = f"{{{htb_hash}}}:{sibling_key}:demand:window"
                sib_demand_counter_k = f"{{{htb_hash}}}:{sibling_key}:demand:requests"
                sib_demand, _ = await _read(sib_demand_window_k, sib_demand_counter_k)
                reservation = min(sib_demand, sibling_limit)
                borrow_ceiling -= reservation
        borrow_ceiling = max(borrow_ceiling, priority_limit)

        if priority_current >= priority_limit and priority_current >= borrow_ceiling:
            return RateLimitResponse(
                overall_code="OVER_LIMIT",
                statuses=[
                    RateLimitStatus(
                        code="OVER_LIMIT",
                        current_limit=priority_limit,
                        limit_remaining=max(0, priority_limit - priority_current),
                        rate_limit_type="requests",
                        descriptor_key="priority_model",
                    )
                ],
            )
        if model_current >= model_limit:
            return RateLimitResponse(
                overall_code="OVER_LIMIT",
                statuses=[
                    RateLimitStatus(
                        code="OVER_LIMIT",
                        current_limit=priority_limit,
                        limit_remaining=max(0, priority_limit - priority_current),
                        rate_limit_type="requests",
                        descriptor_key="priority_model",
                    )
                ],
            )

        new_priority = await _increment(priority_window_key, priority_counter_key, priority_expired)
        new_model = await _increment(model_window_key, model_counter_key, model_expired)

        return RateLimitResponse(
            overall_code="OK",
            statuses=[
                RateLimitStatus(
                    code="OK",
                    current_limit=priority_limit,
                    limit_remaining=max(0, priority_limit - new_priority),
                    rate_limit_type="requests",
                    descriptor_key="priority_model",
                ),
                RateLimitStatus(
                    code="OK",
                    current_limit=model_limit,
                    limit_remaining=max(0, model_limit - new_model),
                    rate_limit_type="requests",
                    descriptor_key="model_saturation_check",
                ),
            ],
        )


class _PROXY_DynamicRateLimitHandlerV3Htb(CustomLogger):
    """
    HTB (Hierarchical Token Bucket) priority-based rate limiter using v3 infrastructure.

    Key features:
    1. Priority usage tracked from first request (accurate accounting)
    2. HTB borrowing: priorities can exceed their guaranteed rate when the
       model has spare capacity
    3. Model-wide RPM is enforced atomically with priority limits (no TOCTOU)
    4. Reuses v3 limiter's Redis-based tracking (multi-instance safe)
    """

    def __init__(
        self,
        internal_usage_cache: Union["InternalUsageCache", DualCache],
        time_provider: Callable[[], datetime] | None = None,
    ):
        if isinstance(internal_usage_cache, DualCache):
            from litellm.proxy.utils import InternalUsageCache

            internal_usage_cache = InternalUsageCache(dual_cache=internal_usage_cache)
        self.internal_usage_cache = internal_usage_cache
        self.v3_limiter = _PROXY_HtbMaxParallelRequestsHandlerV3(self.internal_usage_cache, time_provider=time_provider)

    def update_variables(self, llm_router: Router):
        self.llm_router = llm_router

    def _get_priority_weight(self, priority: str | None, model_info: ModelGroupInfo | None = None) -> float:
        """Get the weight for a given priority from litellm.priority_reservation"""
        weight: float = _get_priority_settings().default_priority
        if litellm.priority_reservation is None or priority not in litellm.priority_reservation:
            verbose_proxy_logger.debug("Priority Reservation not set for the given priority.")
        elif priority is not None and litellm.priority_reservation is not None:
            from litellm.proxy.auth.litellm_license import LicenseCheck

            if not LicenseCheck().is_premium():
                verbose_proxy_logger.error("PREMIUM FEATURE: Reserving tpm/rpm by priority is a premium feature.")
            else:
                value = litellm.priority_reservation[priority]
                weight = convert_priority_to_percent(value, model_info)
        return weight

    def _get_priority_from_user_api_key_dict(self, user_api_key_dict: UserAPIKeyAuth) -> str | None:
        """
        Get priority from user_api_key_dict.

        Checks team metadata first (takes precedence), then falls back to key metadata.
        """
        priority: str | None = None

        if user_api_key_dict.team_metadata is not None:
            priority = user_api_key_dict.team_metadata.get("priority", None)

        if priority is None:
            priority = user_api_key_dict.metadata.get("priority", None)

        return priority

    def _normalize_priority_weights(self, model_info: ModelGroupInfo) -> dict[str, float]:
        """
        Normalize priority weights if they sum to > 1.0
        """
        if litellm.priority_reservation is None:
            return {}

        weights: dict[str, float] = {}
        for k, v in litellm.priority_reservation.items():
            weights[k] = convert_priority_to_percent(v, model_info)

        total_weight = sum(weights.values())

        if total_weight > 1.0:
            normalized = {k: v / total_weight for k, v in weights.items()}
            verbose_proxy_logger.debug(f"Normalized over-allocated priorities: {weights} -> {normalized}")
            return normalized

        return weights

    def _get_priority_allocation(
        self,
        model: str,
        priority: str | None,
        normalized_weights: dict[str, float],
        model_info: ModelGroupInfo | None = None,
    ) -> tuple[float, str]:
        """Get priority weight and pool key for a given priority."""
        has_explicit_priority = (
            priority is not None
            and litellm.priority_reservation is not None
            and priority in litellm.priority_reservation
        )

        if has_explicit_priority and priority is not None:
            priority_weight = normalized_weights.get(priority, self._get_priority_weight(priority, model_info))
            priority_key = f"{model}:{priority}"
        else:
            priority_weight = _get_priority_settings().default_priority
            priority_key = f"{model}:default_pool"

        return priority_weight, priority_key

    def _create_priority_based_descriptors(
        self,
        model: str,
        priority: str | None,
    ) -> list[RateLimitDescriptor]:
        """Create rate limit descriptors with normalized priority weights."""
        descriptors: list[RateLimitDescriptor] = []

        if litellm.priority_reservation is None:
            return descriptors

        model_group_info: ModelGroupInfo | None = self.llm_router.get_model_group_info(model_group=model)
        if model_group_info is None:
            return descriptors

        normalized_weights = self._normalize_priority_weights(model_group_info)
        priority_weight, priority_key = self._get_priority_allocation(
            model=model,
            priority=priority,
            normalized_weights=normalized_weights,
            model_info=model_group_info,
        )

        rate_limit_config: RateLimitDescriptorRateLimitObject = {}

        if model_group_info.tpm is not None:
            reserved_tpm = int(model_group_info.tpm * priority_weight)
            rate_limit_config["tokens_per_unit"] = reserved_tpm

        if model_group_info.rpm is not None:
            reserved_rpm = int(model_group_info.rpm * priority_weight)
            rate_limit_config["requests_per_unit"] = reserved_rpm

        if rate_limit_config:
            rate_limit_config["window_size"] = self.v3_limiter.window_size

            descriptors.append(
                RateLimitDescriptor(
                    key="priority_model",
                    value=priority_key,
                    rate_limit=rate_limit_config,
                )
            )

        return descriptors

    def _create_model_tracking_descriptor(
        self,
        model: str,
        model_group_info: ModelGroupInfo,
        high_limit_multiplier: int = 1,
    ) -> RateLimitDescriptor:
        """Create a descriptor for tracking model-wide usage."""
        return RateLimitDescriptor(
            key="model_saturation_check",
            value=model,
            rate_limit={
                "requests_per_unit": (model_group_info.rpm * high_limit_multiplier if model_group_info.rpm else None),
                "tokens_per_unit": (model_group_info.tpm * high_limit_multiplier if model_group_info.tpm else None),
                "window_size": self.v3_limiter.window_size,
            },
        )

    def _get_sibling_priorities(
        self,
        model: str,
        model_group_info: ModelGroupInfo,
        current_priority: str | None,
    ) -> list[tuple[str, int]]:
        """Build sibling (priority_key, guaranteed_rpm) list for HTB borrow ceiling."""
        if litellm.priority_reservation is None or model_group_info.rpm is None:
            return []

        normalized_weights = self._normalize_priority_weights(model_group_info)
        siblings: list[tuple[str, int]] = []

        for prio_key in litellm.priority_reservation:
            if prio_key == current_priority:
                continue
            weight = normalized_weights.get(prio_key, 0.0)
            guaranteed_rpm = int(model_group_info.rpm * weight)
            sibling_priority_key = f"{model}:{prio_key}"
            siblings.append((sibling_priority_key, guaranteed_rpm))

        return siblings

    async def _run_htb_check(
        self,
        model: str,
        model_group_info: ModelGroupInfo,
        priority: str | None,
        parent_otel_span: Span | None,
    ) -> RateLimitResponse:
        priority_descriptors = self._create_priority_based_descriptors(
            model=model,
            priority=priority,
        )
        if not priority_descriptors:
            return RateLimitResponse(overall_code="OK", statuses=[])

        model_descriptor = self._create_model_tracking_descriptor(
            model=model,
            model_group_info=model_group_info,
            high_limit_multiplier=1,
        )

        sibling_priorities = self._get_sibling_priorities(
            model=model,
            model_group_info=model_group_info,
            current_priority=priority,
        )

        htb_response = await self.v3_limiter.htb_check_and_increment(
            priority_descriptor=priority_descriptors[0],
            model_descriptor=model_descriptor,
            parent_otel_span=parent_otel_span,
            sibling_priorities=sibling_priorities,
            saturation_threshold=_get_priority_settings().saturation_threshold,
        )

        verbose_proxy_logger.debug(f"[HTB] Model={model}, Priority={priority}, Response={htb_response['overall_code']}")
        return htb_response

    def _raise_rate_limit_error(
        self,
        model: str,
        model_group_info: ModelGroupInfo,
        priority: str | None,
        htb_response: RateLimitResponse,
    ) -> None:
        resolved_model, llm_provider = resolve_llm_provider_for_rate_limit(model)
        status = next(
            (s for s in htb_response["statuses"] if s["code"] == "OVER_LIMIT"),
            None,
        )
        rate_limit_type = str(status["rate_limit_type"]) if status else "unknown"
        limit_remaining = status["limit_remaining"] if status else 0
        raise litellm.RateLimitError(
            message=f"Priority-based rate limit exceeded. "
            f"Model: {model}, "
            f"Priority: {priority}, "
            f"Rate limit type: {rate_limit_type}, "
            f"Model RPM: {model_group_info.rpm if model_group_info.rpm is not None else 'not configured'}, "
            f"Remaining: {limit_remaining}",
            llm_provider=llm_provider,
            model=resolved_model,
            response=httpx.Response(
                status_code=429,
                content=f"Priority rate limit exceeded for model={model}, priority={priority}",
                headers={"retry-after": str(self.v3_limiter.window_size)},
                request=httpx.Request(
                    method="htb_pre_call_check",
                    url="https://github.com/BerriAI/litellm",
                ),
            ),
            num_retries=0,
        )

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: CallTypesLiteral,
    ) -> Exception | str | dict | None:
        if "model" not in data:
            return None
        priority = self._get_priority_from_user_api_key_dict(user_api_key_dict=user_api_key_dict)
        htb_priority.set(priority)
        return None

    async def async_pre_call_check(self, deployment: dict, parent_otel_span: Span | None) -> dict | None:
        if litellm.priority_reservation is None:
            return deployment

        priority = htb_priority.get()

        model_group = deployment.get("model_name", "")
        if not model_group:
            return deployment

        model_group_info: ModelGroupInfo | None = self.llm_router.get_model_group_info(model_group=model_group)
        if model_group_info is None:
            return deployment
        if model_group_info.rpm is None and model_group_info.tpm is None:
            return deployment

        try:
            htb_response = await self._run_htb_check(
                model=model_group,
                model_group_info=model_group_info,
                priority=priority,
                parent_otel_span=parent_otel_span,
            )
        except Exception as e:  # noqa: BLE001 - fail-open on HTB check errors, allow request
            verbose_proxy_logger.error(f"[HTB] async_pre_call_check error: {e}, allowing request")
            return deployment

        if htb_response["overall_code"] != "OVER_LIMIT":
            return deployment

        self._raise_rate_limit_error(
            model=model_group,
            model_group_info=model_group_info,
            priority=priority,
            htb_response=htb_response,
        )

    def pre_call_check(self, deployment: dict) -> dict | None:
        return deployment

    async def async_post_call_success_hook(self, data: dict, user_api_key_dict: UserAPIKeyAuth, response):
        """Add rate limit headers and priority info to the response."""
        try:
            await self.v3_limiter.async_post_call_success_hook(
                data=data, user_api_key_dict=user_api_key_dict, response=response
            )

            if isinstance(response, ModelResponse):
                priority = self._get_priority_from_user_api_key_dict(user_api_key_dict=user_api_key_dict)

                additional_headers = getattr(response, "_hidden_params", {}).get("additional_headers", {}) or {}

                additional_headers["x-litellm-priority"] = priority or "default"
                additional_headers["x-litellm-rate-limiter-version"] = "v3"

                if not hasattr(response, "_hidden_params"):
                    response._hidden_params = {}
                response._hidden_params["additional_headers"] = additional_headers

            return response

        except Exception as e:  # noqa: BLE001 - best-effort response decoration never fails the request
            verbose_proxy_logger.exception(f"Error in dynamic rate limiter v3 post-call hook: {e!s}")
            return response

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """
        Update token usage for priority-based rate limiting after successful API calls.
        """
        from litellm.litellm_core_utils.core_helpers import (
            _get_parent_otel_span_from_kwargs,
        )
        from litellm.proxy.common_utils.callback_utils import (
            get_model_group_from_litellm_kwargs,
        )
        from litellm.types.caching import RedisPipelineIncrementOperation
        from litellm.types.utils import Usage

        try:
            verbose_proxy_logger.debug("INSIDE dynamic rate limiter ASYNC SUCCESS LOGGING")

            litellm_parent_otel_span = _get_parent_otel_span_from_kwargs(kwargs)

            standard_logging_object = kwargs.get("standard_logging_object") or {}
            standard_logging_metadata = standard_logging_object.get("metadata") or {}

            model_group = get_model_group_from_litellm_kwargs(kwargs)
            if not model_group:
                return

            user_api_key_auth_metadata = standard_logging_metadata.get("user_api_key_auth_metadata") or {}
            key_priority: str | None = user_api_key_auth_metadata.get("priority")

            total_tokens = 0
            rate_limit_type = self.v3_limiter.get_rate_limit_type()

            if isinstance(response_obj, ModelResponse):
                _usage = getattr(response_obj, "usage", None)
                if _usage and isinstance(_usage, Usage):
                    if rate_limit_type == "output":
                        total_tokens = _usage.completion_tokens
                    elif rate_limit_type == "input":
                        total_tokens = _usage.prompt_tokens
                    elif rate_limit_type == "total":
                        total_tokens = _usage.total_tokens

            if total_tokens == 0:
                return

            pipeline_operations: list[RedisPipelineIncrementOperation] = []

            model_token_key = self.v3_limiter.create_rate_limit_keys(
                key="model_saturation_check",
                value=model_group,
                rate_limit_type="tokens",
            )
            pipeline_operations.append(
                RedisPipelineIncrementOperation(
                    key=model_token_key,
                    increment_value=total_tokens,
                    ttl=self.v3_limiter.window_size,
                )
            )

            has_explicit_priority = (
                key_priority is not None
                and litellm.priority_reservation is not None
                and key_priority in litellm.priority_reservation
            )

            if has_explicit_priority and key_priority is not None:
                priority_key = f"{model_group}:{key_priority}"
            else:
                priority_key = f"{model_group}:default_pool"

            priority_token_key = self.v3_limiter.create_rate_limit_keys(
                key="priority_model",
                value=priority_key,
                rate_limit_type="tokens",
            )
            pipeline_operations.append(
                RedisPipelineIncrementOperation(
                    key=priority_token_key,
                    increment_value=total_tokens,
                    ttl=self.v3_limiter.window_size,
                )
            )

            if pipeline_operations:
                await self.v3_limiter.async_increment_tokens_with_ttl_preservation(
                    pipeline_operations=pipeline_operations,
                    parent_otel_span=litellm_parent_otel_span,
                )

                SAFE_PRIORITIES = {"low", "medium", "high", "default"}
                logged_priority = key_priority if key_priority in SAFE_PRIORITIES else "REDACTED"
                verbose_proxy_logger.debug(
                    f"[Dynamic Rate Limiter] Incremented tokens by {total_tokens} for "
                    f"model={model_group}, priority={logged_priority}"
                )

        except Exception as e:  # noqa: BLE001 - success-event logging must never break the request
            verbose_proxy_logger.exception(f"Error in dynamic rate limiter success event: {e!s}")
