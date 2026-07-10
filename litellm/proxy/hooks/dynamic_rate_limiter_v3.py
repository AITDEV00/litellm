"""
Dynamic rate limiter v3 - Saturation-aware priority-based rate limiting
"""

from datetime import datetime
from typing import TYPE_CHECKING, Callable, Dict, List, Literal, Optional, Union

from fastapi import HTTPException

import litellm
from litellm import ModelResponse, Router
from litellm._logging import verbose_proxy_logger
from litellm.caching.caching import DualCache
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.common_utils.proxy_rate_limit_error import (
    ProxyRateLimitError,
    map_v3_rate_limit_type,
)
from litellm.proxy.hooks.parallel_request_limiter_v3 import (
    RateLimitDescriptor,
    RateLimitDescriptorRateLimitObject,
    _PROXY_MaxParallelRequestsHandler_v3,
)
from litellm.proxy.hooks.rate_limiter_utils import (
    convert_priority_to_percent,
    resolve_llm_provider_for_rate_limit,
)
from litellm.types.router import ModelGroupInfo
from litellm.types.utils import CallTypesLiteral

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
        # This should never happen due to lazy loading, but satisfy mypy
        from litellm.types.utils import PriorityReservationSettings

        return PriorityReservationSettings()
    return settings


class _PROXY_DynamicRateLimitHandlerV3(CustomLogger):
    """
    Saturation-aware priority-based rate limiter using v3 infrastructure.

    Key features:
    1. Priority usage tracked from first request (accurate accounting)
    2. Priority limits only enforced when saturated >= threshold
    3. Model-wide RPM is NOT enforced here; the router's
       ``enforce_model_rate_limits`` pre-call check handles that and
       triggers fallbacks when a deployment is at capacity
    4. Reuses v3 limiter's Redis-based tracking (multi-instance safe)

    How it works:
    - When under-saturated: all priorities can borrow full model capacity
    - When saturated: strict priority-based limits enforced (fair)
    - Uses v3 limiter's atomic Lua scripts for race-free increments
    """

    def __init__(
        self,
        internal_usage_cache: Union["InternalUsageCache", DualCache],
        time_provider: Optional[Callable[[], datetime]] = None,
    ):
        if isinstance(internal_usage_cache, DualCache):
            from litellm.proxy.utils import InternalUsageCache

            internal_usage_cache = InternalUsageCache(dual_cache=internal_usage_cache)
        self.internal_usage_cache = internal_usage_cache
        self.v3_limiter = _PROXY_MaxParallelRequestsHandler_v3(
            self.internal_usage_cache, time_provider=time_provider
        )

    def update_variables(self, llm_router: Router):
        self.llm_router = llm_router

    def _model_has_fallbacks(self, model: str) -> bool:
        """Check whether the specific model group has fallbacks configured.

        When fallbacks are configured, priority enforcement defers to the
        router instead of raising at the proxy level. This lets the router's
        ``enforce_model_rate_limits`` pre-call check block the primary
        deployment and trigger the fallback chain.
        """
        if self.llm_router is None:
            return False
        if self.llm_router.fallbacks:
            for fb_entry in self.llm_router.fallbacks:
                if isinstance(fb_entry, dict) and (model in fb_entry or "*" in fb_entry):
                    return True
        deployments = self.llm_router.get_model_list(model_name=model) or []
        for d in deployments:
            lp = d.get("litellm_params") or {}
            if lp.get("fallbacks"):
                return True
        return False

    def _get_saturation_check_cache_ttl(self) -> int:
        """Get the configurable TTL for local cache when reading saturation values."""
        return _get_priority_settings().saturation_check_cache_ttl

    async def _get_saturation_value_from_cache(
        self,
        counter_key: str,
    ) -> Optional[str]:
        """
        Get saturation value with configurable local cache TTL.

        Uses DualCache with configurable TTL for local cache storage.
        TTL is configurable via litellm.priority_reservation_settings.saturation_check_cache_ttl

        Args:
            counter_key: The cache key for the saturation counter

        Returns:
            Counter value as string, or None if not found
        """
        local_cache_ttl = self._get_saturation_check_cache_ttl()

        return await self.internal_usage_cache.async_get_cache(
            key=counter_key,
            litellm_parent_otel_span=None,
            local_only=False,
            ttl=local_cache_ttl,
        )

    def _get_priority_weight(self, priority: Optional[str], model_info: Optional[ModelGroupInfo] = None) -> float:
        """Get the weight for a given priority from litellm.priority_reservation"""
        weight: float = _get_priority_settings().default_priority
        if litellm.priority_reservation is None or priority not in litellm.priority_reservation:
            verbose_proxy_logger.debug("Priority Reservation not set for the given priority.")
        elif priority is not None and litellm.priority_reservation is not None:
            from litellm.proxy.auth.litellm_license import LicenseCheck

            if not LicenseCheck().is_premium():
                verbose_proxy_logger.error(
                    "PREMIUM FEATURE: Reserving tpm/rpm by priority is a premium feature."
                )
            else:
                value = litellm.priority_reservation[priority]
                weight = convert_priority_to_percent(value, model_info)
        return weight

    def _get_priority_from_user_api_key_dict(self, user_api_key_dict: UserAPIKeyAuth) -> Optional[str]:
        """
        Get priority from user_api_key_dict.

        Checks team metadata first (takes precedence), then falls back to key metadata.

        Args:
            user_api_key_dict: User authentication info

        Returns:
            Priority string if found, None otherwise
        """
        priority: Optional[str] = None

        # Check team metadata first (takes precedence)
        if user_api_key_dict.team_metadata is not None:
            priority = user_api_key_dict.team_metadata.get("priority", None)

        # Fall back to key metadata
        if priority is None:
            priority = user_api_key_dict.metadata.get("priority", None)

        return priority

    def _normalize_priority_weights(self, model_info: ModelGroupInfo) -> Dict[str, float]:
        """
        Normalize priority weights if they sum to > 1.0

        Handles over-allocation: {key_a: 0.60, key_b: 0.80} -> {key_a: 0.43, key_b: 0.57}
        Converts absolute rpm/tpm values to percentages based on model capacity.
        """
        if litellm.priority_reservation is None:
            return {}

        # Convert all values to percentages first
        weights: Dict[str, float] = {}
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
        priority: Optional[str],
        normalized_weights: Dict[str, float],
        model_info: Optional[ModelGroupInfo] = None,
    ) -> tuple[float, str]:
        """
        Get priority weight and pool key for a given priority.

        For explicit priorities: returns specific allocation and unique pool key
        For default priority: returns default allocation and shared pool key

        Args:
            model: Model name
            priority: Priority level (None for default)
            normalized_weights: Pre-computed normalized weights
            model_info: Model configuration (optional, for fallback conversion)

        Returns:
            tuple: (priority_weight, priority_key)
        """
        # Check if this key has an explicit priority in litellm.priority_reservation
        has_explicit_priority = (
            priority is not None
            and litellm.priority_reservation is not None
            and priority in litellm.priority_reservation
        )

        if has_explicit_priority and priority is not None:
            # Explicit priority: get its specific allocation
            priority_weight = normalized_weights.get(priority, self._get_priority_weight(priority, model_info))
            # Use unique key per priority level
            priority_key = f"{model}:{priority}"
        else:
            # No explicit priority: share the default_priority pool with ALL other default keys
            priority_weight = _get_priority_settings().default_priority
            # Use shared key for all default-priority requests
            priority_key = f"{model}:default_pool"

        return priority_weight, priority_key

    async def _check_model_saturation(
        self,
        model: str,
        model_group_info: ModelGroupInfo,
    ) -> float:
        """
        Check current saturation by directly querying v3 limiter's cache keys.

        Reuses v3 limiter's Redis-based tracking (works across multiple instances).
        Reads counters WITHOUT incrementing them.

        Returns:
            float: Saturation ratio (0.0 = empty, 1.0 = at capacity, >1.0 = over)
        """
        try:
            max_saturation = 0.0

            # Query RPM saturation - always read from Redis for multi-node consistency
            if model_group_info.rpm is not None and model_group_info.rpm > 0:
                # Use v3 limiter's key format: {key:value}:rate_limit_type
                counter_key = self.v3_limiter.create_rate_limit_keys(
                    key="model_saturation_check",
                    value=model,
                    rate_limit_type="requests",
                )

                # Query Redis directly for current counter value (skip local cache for consistency)
                counter_value = await self._get_saturation_value_from_cache(counter_key=counter_key)

                if counter_value is not None:
                    current_requests = int(counter_value)
                    rpm_saturation = current_requests / model_group_info.rpm
                    max_saturation = max(max_saturation, rpm_saturation)

                    verbose_proxy_logger.debug(
                        f"Model {model} RPM: {current_requests}/{model_group_info.rpm} ({rpm_saturation:.1%})"
                    )

            # Query TPM saturation
            if model_group_info.tpm is not None and model_group_info.tpm > 0:
                counter_key = self.v3_limiter.create_rate_limit_keys(
                    key="model_saturation_check",
                    value=model,
                    rate_limit_type="tokens",
                )

                counter_value = await self._get_saturation_value_from_cache(counter_key=counter_key)

                if counter_value is not None:
                    current_tokens = float(counter_value)
                    tpm_saturation = current_tokens / model_group_info.tpm
                    max_saturation = max(max_saturation, tpm_saturation)

                    verbose_proxy_logger.debug(
                        f"Model {model} TPM: {current_tokens}/{model_group_info.tpm} ({tpm_saturation:.1%})"
                    )

            verbose_proxy_logger.debug(f"Model {model} overall saturation: {max_saturation:.1%}")

            return max_saturation

        except Exception as e:
            verbose_proxy_logger.error(f"Error checking saturation for {model}: {str(e)}")
            # Fail open: assume not saturated on error
            return 0.0

    def _compute_saturation_from_response(
        self,
        model_group_info: ModelGroupInfo,
        atomic_response: dict,
    ) -> float:
        """
        Compute saturation from the post-increment atomic response.

        The model-wide tracking descriptor uses a high limit multiplier (so
        the counter never blocks), but saturation is computed against the
        real model RPM/TPM. We recover the actual counter value as
        ``current_limit - limit_remaining`` and divide by the real limit.
        """
        try:
            max_saturation = 0.0
            for status in atomic_response.get("statuses", []):
                descriptor_key = status.get("descriptor_key", "")
                if descriptor_key != "model_saturation_check":
                    continue

                rate_limit_type = status.get("rate_limit_type", "")
                current_limit = status.get("current_limit", 0)
                limit_remaining = status.get("limit_remaining", 0)
                counter = current_limit - limit_remaining

                if rate_limit_type == "requests" and model_group_info.rpm:
                    saturation = counter / model_group_info.rpm
                    max_saturation = max(max_saturation, saturation)
                elif rate_limit_type == "tokens" and model_group_info.tpm:
                    saturation = counter / model_group_info.tpm
                    max_saturation = max(max_saturation, saturation)

            return max_saturation

        except Exception:
            return 0.0

    def _create_priority_based_descriptors(
        self,
        model: str,
        user_api_key_dict: UserAPIKeyAuth,
        priority: Optional[str],
    ) -> List[RateLimitDescriptor]:
        """
        Create rate limit descriptors with normalized priority weights.

        Uses normalized weights to handle over-allocation scenarios.

        For explicit priorities: each priority gets its own pool (e.g., prod gets 75%)
        For default priority: ALL keys without explicit priority share ONE pool (e.g., all share 25%)
        """
        descriptors: List[RateLimitDescriptor] = []

        if litellm.priority_reservation is None:
            return descriptors

        # Get model group info
        model_group_info: Optional[ModelGroupInfo] = self.llm_router.get_model_group_info(model_group=model)
        if model_group_info is None:
            return descriptors

        # Get normalized priority weight and pool key
        normalized_weights = self._normalize_priority_weights(model_group_info)
        priority_weight, priority_key = self._get_priority_allocation(
            model=model,
            priority=priority,
            normalized_weights=normalized_weights,
            model_info=model_group_info,
        )

        rate_limit_config: RateLimitDescriptorRateLimitObject = {}

        # Apply priority weight to model limits
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
        """
        Create a descriptor for tracking model-wide usage.

        Args:
            model: Model name
            model_group_info: Model configuration with RPM/TPM limits
            high_limit_multiplier: Multiplier for limits (use >1 for tracking-only)

        Returns:
            Rate limit descriptor for model-wide tracking
        """
        return RateLimitDescriptor(
            key="model_saturation_check",
            value=model,
            rate_limit={
                "requests_per_unit": (model_group_info.rpm * high_limit_multiplier if model_group_info.rpm else None),
                "tokens_per_unit": (model_group_info.tpm * high_limit_multiplier if model_group_info.tpm else None),
                "window_size": self.v3_limiter.window_size,
            },
        )

    async def _check_priority_limits(
        self,
        model: str,
        model_group_info: ModelGroupInfo,
        user_api_key_dict: UserAPIKeyAuth,
        priority: Optional[str],
        saturation: float,
        data: dict,
    ) -> None:
        """
        Priority-aware rate limiting that defers model-wide RPM enforcement to
        the router so fallbacks can take over when the primary model is full.

        Model-wide RPM is never enforced here. The router's
        ``enforce_model_rate_limits`` pre-call check (configured via
        ``optional_pre_call_checks``) handles that and correctly triggers
        fallbacks when a deployment is at capacity.

        This hook only enforces priority-based limits.

        Enforcement rules:
        - Model WITH fallbacks: enforce priority only when saturation >=
          threshold. Below the threshold, all priorities can borrow the
          model's full capacity. When priority limit is exceeded and
          fallbacks are configured, the hook defers to the router, which
          blocks the primary deployment and triggers the fallback chain.
        - Model WITHOUT fallbacks: always enforce priority caps, regardless
          of saturation. Each priority gets its reserved share from the
          start (e.g. prior3 capped at 20 RPM, prior1 gets 50 RPM), so
          low-priority traffic cannot starve high-priority traffic.

        Args:
            model: Model name
            model_group_info: Model configuration
            user_api_key_dict: User authentication info
            priority: User's priority level
            saturation: Current saturation level (post model-counter increment)
            data: Request data dictionary

        Raises:
            HTTPException: If priority-based limit is exceeded and no fallbacks
                are configured for the model
        """
        import json

        saturation_threshold = _get_priority_settings().saturation_threshold
        has_fallbacks = self._model_has_fallbacks(model)
        should_enforce_priority = saturation >= saturation_threshold or not has_fallbacks

        priority_descriptors = self._create_priority_based_descriptors(
            model=model,
            user_api_key_dict=user_api_key_dict,
            priority=priority,
        )

        per_request_increment: Dict[Literal["requests", "tokens"], int] = {
            "requests": 1,
            "tokens": 0,
        }

        if should_enforce_priority and priority_descriptors:
            enforced_descriptors: List[RateLimitDescriptor] = list(priority_descriptors)
            atomic_response = await self.v3_limiter.atomic_check_and_increment_by_n(
                descriptors=enforced_descriptors,
                increments=[per_request_increment for _ in enforced_descriptors],
                parent_otel_span=user_api_key_dict.parent_otel_span,
            )

            verbose_proxy_logger.debug(
                f"Priority atomic check+increment response: {json.dumps(atomic_response, indent=2)}"
            )

            if atomic_response["overall_code"] == "OVER_LIMIT":
                resolved_model, llm_provider = resolve_llm_provider_for_rate_limit(model)
                for status in atomic_response["statuses"]:
                    if status["code"] != "OVER_LIMIT":
                        continue
                    if status["descriptor_key"] == "priority_model":
                        if has_fallbacks:
                            verbose_proxy_logger.info(
                                f"Priority limit exceeded for {model} (priority={priority}, "
                                f"saturation={saturation:.1%}) but fallbacks configured; "
                                f"deferring to router for fallback handling"
                            )
                            data["litellm_proxy_rate_limit_response"] = atomic_response
                            return
                        verbose_proxy_logger.debug(
                            f"Enforcing priority limits for {model}, saturation: {saturation:.1%}, "
                            f"priority: {priority}"
                        )
                        raise ProxyRateLimitError(
                            detail={
                                "error": f"Priority-based rate limit exceeded. "
                                f"Model: {model}, "
                                f"Priority: {priority}, "
                                f"Rate limit type: {status['rate_limit_type']}, "
                                f"Model TPM: {model_group_info.tpm if model_group_info.tpm is not None else 'not configured'}, "
                                f"Model RPM: {model_group_info.rpm if model_group_info.rpm is not None else 'not configured'}, "
                                f"Remaining: {status['limit_remaining']}, "
                                f"Model saturation: {saturation:.1%}"
                            },
                            headers={
                                "retry-after": str(self.v3_limiter.window_size),
                                "rate_limit_type": str(status["rate_limit_type"]),
                                "x-litellm-priority": priority or "default",
                                "x-litellm-saturation": f"{saturation:.2%}",
                            },
                            rate_limit_type=map_v3_rate_limit_type(status["rate_limit_type"]),
                            model=resolved_model,
                            llm_provider=llm_provider,
                        )

                offending = next(
                    (s for s in atomic_response["statuses"] if s["code"] == "OVER_LIMIT"),
                    None,
                )
                verbose_proxy_logger.error(
                    f"Dynamic rate limiter: OVER_LIMIT response with unknown "
                    f"descriptor_key(s) — refusing request. response={atomic_response}"
                )
                raise ProxyRateLimitError(
                    detail={
                        "error": "Rate limit exceeded",
                        "descriptor_key": (offending["descriptor_key"] if offending else "unknown"),
                        "rate_limit_type": (str(offending["rate_limit_type"]) if offending else "unknown"),
                    },
                    rate_limit_type=map_v3_rate_limit_type(offending["rate_limit_type"] if offending else None),
                    headers={
                        "retry-after": str(self.v3_limiter.window_size),
                        "x-litellm-priority": priority or "default",
                    },
                    model=resolved_model,
                    llm_provider=llm_provider,
                )

            data["litellm_proxy_rate_limit_response"] = atomic_response
        else:
            if priority_descriptors:
                tracking_response = await self.v3_limiter.should_rate_limit(
                    descriptors=priority_descriptors,
                    parent_otel_span=user_api_key_dict.parent_otel_span,
                    read_only=False,
                )

                verbose_proxy_logger.debug(
                    f"Tracking-only response (priority not enforced): {json.dumps(tracking_response, indent=2)}"
                )

                data["litellm_proxy_rate_limit_response"] = tracking_response

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict,
        call_type: CallTypesLiteral,
    ) -> Optional[Union[Exception, str, dict]]:
        """
        Saturation-aware pre-call hook for priority-based rate limiting.

        Flow:
        1. Atomically increment the model-wide tracking counter (high limit,
           never blocks) and compute saturation from the post-increment value
        2. If saturation >= threshold (model with fallbacks) OR always (model
           without fallbacks): atomically check+increment the priority counter
           and raise 429 if over the priority-specific limit
        3. If below threshold and model has fallbacks: increment the priority
           counter for tracking only

        Model-wide RPM is NOT enforced here. The router's
        ``enforce_model_rate_limits`` pre-call check handles that and
        triggers fallbacks when a deployment is at capacity.

        Example with 100 RPM model, 20% priority allocation (prior3), 80% threshold:
        - Model WITH fallbacks, saturation < 80%: prior3 can use up to 100 RPM
        - Model WITH fallbacks, saturation >= 80%: prior3 capped at 20 RPM
        - Model WITHOUT fallbacks: prior3 always capped at 20 RPM

        Args:
            user_api_key_dict: User authentication and metadata
            cache: Dual cache instance
            data: Request data containing model name
            call_type: Type of API call being made

        Returns:
            None if request is allowed, otherwise raises HTTPException
        """
        if "model" not in data:
            return None

        model = data["model"]
        priority = self._get_priority_from_user_api_key_dict(user_api_key_dict=user_api_key_dict)

        # Get model configuration
        model_group_info: Optional[ModelGroupInfo] = self.llm_router.get_model_group_info(model_group=model)
        if model_group_info is None:
            verbose_proxy_logger.debug(f"No model group info for {model}, allowing request")
            return None

        try:
            # Atomically increment the model-wide tracking counter FIRST, then
            # use the returned (post-increment) value to compute saturation.
            # Using a high limit multiplier ensures the counter never returns
            # OVER_LIMIT (which would skip the increment), so every request
            # is tracked and concurrent requests see an accurate, monotonically
            # increasing saturation level.
            model_wide_descriptor = self._create_model_tracking_descriptor(
                model=model,
                model_group_info=model_group_info,
                high_limit_multiplier=10000,
            )

            model_increment: Dict[Literal["requests", "tokens"], int] = {
                "requests": 1,
                "tokens": 0,
            }

            model_atomic_response = (
                await self.v3_limiter.atomic_check_and_increment_by_n(
                    descriptors=[model_wide_descriptor],
                    increments=[model_increment],
                    parent_otel_span=user_api_key_dict.parent_otel_span,
                )
            )

            # Compute saturation from the post-increment counter value
            saturation = self._compute_saturation_from_response(
                model_group_info=model_group_info,
                atomic_response=model_atomic_response,
            )

            saturation_threshold = _get_priority_settings().saturation_threshold

            verbose_proxy_logger.debug(
                f"[Dynamic Rate Limiter] Model={model}, Saturation={saturation:.1%}, "
                f"Threshold={saturation_threshold:.1%}, Priority={priority}"
            )

            # Enforce priority limits if saturated, track priority usage
            await self._check_priority_limits(
                model=model,
                model_group_info=model_group_info,
                user_api_key_dict=user_api_key_dict,
                priority=priority,
                saturation=saturation,
                data=data,
            )

        except HTTPException:
            raise
        except Exception as e:
            verbose_proxy_logger.error(f"Error in dynamic rate limiter: {str(e)}, allowing request")
            # Fail open on unexpected errors
            return None

        return None

    async def async_post_call_success_hook(self, data: dict, user_api_key_dict: UserAPIKeyAuth, response):
        """
        Post-call hook to add rate limit headers to response.
        Leverages v3 limiter's post-call hook functionality.
        """
        try:
            # Call v3 limiter's post-call hook to add standard rate limit headers
            await self.v3_limiter.async_post_call_success_hook(
                data=data, user_api_key_dict=user_api_key_dict, response=response
            )

            # Add additional priority-specific headers
            if isinstance(response, ModelResponse):
                priority = self._get_priority_from_user_api_key_dict(user_api_key_dict=user_api_key_dict)

                # Get existing additional headers
                additional_headers = getattr(response, "_hidden_params", {}).get("additional_headers", {}) or {}

                # Add priority information
                additional_headers["x-litellm-priority"] = priority or "default"
                additional_headers["x-litellm-rate-limiter-version"] = "v3"

                # Update response
                if not hasattr(response, "_hidden_params"):
                    response._hidden_params = {}
                response._hidden_params["additional_headers"] = additional_headers

            return response

        except Exception as e:
            verbose_proxy_logger.exception(f"Error in dynamic rate limiter v3 post-call hook: {str(e)}")
            return response

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """
        Update token usage for priority-based rate limiting after successful API calls.

        Increments token counters for:
        - model_saturation_check: Model-wide token tracking
        - priority_model: Priority-specific token tracking
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

            # Get metadata from standard_logging_object
            standard_logging_object = kwargs.get("standard_logging_object") or {}
            standard_logging_metadata = standard_logging_object.get("metadata") or {}

            # Get model and priority
            model_group = get_model_group_from_litellm_kwargs(kwargs)
            if not model_group:
                return

            # Get priority from user_api_key_auth_metadata in standard_logging_metadata
            # This is where user_api_key_dict.metadata is stored during pre-call
            user_api_key_auth_metadata = standard_logging_metadata.get("user_api_key_auth_metadata") or {}
            key_priority: Optional[str] = user_api_key_auth_metadata.get("priority")

            # Get total tokens from response
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

            # Create pipeline operations for token increments
            pipeline_operations: List[RedisPipelineIncrementOperation] = []

            # Model-wide token tracking (model_saturation_check)
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

            # Priority-specific token tracking (priority_model)
            # Determine priority key (same logic as _get_priority_allocation)
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

            # Execute token increments with TTL preservation
            if pipeline_operations:
                await self.v3_limiter.async_increment_tokens_with_ttl_preservation(
                    pipeline_operations=pipeline_operations,
                    parent_otel_span=litellm_parent_otel_span,
                )

                # Only log 'priority' if it's known safe; otherwise, redact.
                SAFE_PRIORITIES = {"low", "medium", "high", "default"}
                logged_priority = key_priority if key_priority in SAFE_PRIORITIES else "REDACTED"
                verbose_proxy_logger.debug(
                    f"[Dynamic Rate Limiter] Incremented tokens by {total_tokens} for "
                    f"model={model_group}, priority={logged_priority}"
                )

        except Exception as e:
            verbose_proxy_logger.exception(f"Error in dynamic rate limiter success event: {str(e)}")
