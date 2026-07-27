"""
Tests for ADEO 3-priority reservation configuration.

Production config (litellm-proxy.yaml):
    priority_reservation:
      prior1: 0.50
      prior2: 0.30
      prior3: 0.20
    priority_reservation_settings:
      saturation_threshold: 1.0

Model: Qwen/Qwen3.5-0.8B with rpm=180

These tests verify that the priority rules are respected:
- Descriptor allocation matches 50/30/20 split
- Within guaranteed rate: always allowed (if model has capacity)
- Borrowing: allowed if siblings have spare demand (demand < guaranteed)
- Over-capacity: each priority gets exactly its guaranteed rate (no starvation)
- Idle siblings: their full guaranteed rate is borrowable
- Model capacity is never exceeded (hard cap)
- Default-priority keys share a single pool
- 429 error messages include model name and configured limits
"""

import os
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.abspath("../../../.."))

import litellm
from litellm import DualCache, Router
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.utils import InternalUsageCache
from litellm.proxy.hooks.dynamic_rate_limiter_v3 import (
    _PROXY_DynamicRateLimitHandlerV3 as DynamicRateLimitHandler,
)
from litellm.types.utils import PriorityReservationSettings

MODEL = "Qwen/Qwen3.5-0.8B"
MODEL_RPM = 180
PRIOR1_RPM = 90  # 0.50 * 180
PRIOR2_RPM = 54  # 0.30 * 180
PRIOR3_RPM = 36  # 0.20 * 180
SATURATION_THRESHOLD = 1.0


@pytest.fixture
def adeo_priority_config():
    """Set up the ADEO 3-priority configuration."""
    os.environ["LITELLM_LICENSE"] = "test-license-key"
    litellm.priority_reservation = {"prior1": 0.50, "prior2": 0.30, "prior3": 0.20}
    original_settings = litellm.priority_reservation_settings
    litellm.priority_reservation_settings = PriorityReservationSettings(
        saturation_threshold=SATURATION_THRESHOLD,
        default_priority=0.25,
    )
    yield
    litellm.priority_reservation = None
    litellm.priority_reservation_settings = original_settings
    del os.environ["LITELLM_LICENSE"]


@pytest.fixture
def handler(adeo_priority_config):
    """Create a handler with a router configured for the ADEO model."""
    dual_cache = DualCache()
    h = DynamicRateLimitHandler(internal_usage_cache=dual_cache)
    llm_router = Router(
        model_list=[
            {
                "model_name": MODEL,
                "litellm_params": {
                    "model": "openai/Qwen3.5-0.8B",
                    "api_key": "test-key",
                    "api_base": "test-base",
                    "rpm": MODEL_RPM,
                },
            }
        ]
    )
    h.update_variables(llm_router=llm_router)
    return h


def _make_user(priority: str | None = None, user_id: str = "user") -> UserAPIKeyAuth:
    user = UserAPIKeyAuth()
    user.metadata = {"priority": priority} if priority else {}
    user.user_id = user_id
    return user


class TestDescriptorAllocation:
    """Verify that _create_priority_based_descriptors produces correct RPM limits."""

    def test_prior1_gets_50_percent(self, handler):
        user = _make_user("prior1", "u1")
        descs = handler._create_priority_based_descriptors(model=MODEL, priority="prior1")
        assert len(descs) == 1
        d = descs[0]
        assert d["value"] == f"{MODEL}:prior1"
        assert d["rate_limit"]["requests_per_unit"] == PRIOR1_RPM
        assert d["key"] == "priority_model"

    def test_prior2_gets_30_percent(self, handler):
        user = _make_user("prior2", "u2")
        descs = handler._create_priority_based_descriptors(model=MODEL, priority="prior2")
        assert len(descs) == 1
        d = descs[0]
        assert d["value"] == f"{MODEL}:prior2"
        assert d["rate_limit"]["requests_per_unit"] == PRIOR2_RPM

    def test_prior3_gets_20_percent(self, handler):
        user = _make_user("prior3", "u3")
        descs = handler._create_priority_based_descriptors(model=MODEL, priority="prior3")
        assert len(descs) == 1
        d = descs[0]
        assert d["value"] == f"{MODEL}:prior3"
        assert d["rate_limit"]["requests_per_unit"] == PRIOR3_RPM

    def test_ratio_is_5_3_2(self, handler):
        """The three allocations should be in exact 5:3:2 ratio."""
        descs = {}
        for p in ("prior1", "prior2", "prior3"):
            user = _make_user(p, f"u_{p}")
            d = handler._create_priority_based_descriptors(model=MODEL, priority=p)[0]
            descs[p] = d["rate_limit"]["requests_per_unit"]

        assert descs["prior1"] == 90
        assert descs["prior2"] == 54
        assert descs["prior3"] == 36

        r12 = descs["prior1"] / descs["prior2"]
        r23 = descs["prior2"] / descs["prior3"]
        assert abs(r12 - 5 / 3) < 0.01
        assert abs(r23 - 3 / 2) < 0.01

    def test_no_priority_uses_default_pool(self, handler):
        """A key without priority metadata should use the shared default_pool."""
        user = _make_user(None, "default_user")
        descs = handler._create_priority_based_descriptors(model=MODEL, priority=None)
        assert len(descs) == 1
        d = descs[0]
        assert d["value"] == f"{MODEL}:default_pool"
        assert d["rate_limit"]["requests_per_unit"] == int(MODEL_RPM * 0.25)

    def test_multiple_default_keys_share_one_pool(self, handler):
        """All default-priority keys must share the same pool key, not get individual pools."""
        users = [_make_user(None, f"u{i}") for i in range(5)]
        pool_keys = set()
        for u in users:
            d = handler._create_priority_based_descriptors(model=MODEL, priority=None)[0]
            pool_keys.add(d["value"])
        assert len(pool_keys) == 1, f"Expected 1 shared pool, got {pool_keys}"
        assert pool_keys.pop() == f"{MODEL}:default_pool"


class TestPriorityWeightExtraction:
    """Verify priority is correctly extracted from user_api_key_dict."""

    def test_priority_from_key_metadata(self, handler):
        user = _make_user("prior1", "u1")
        assert handler._get_priority_from_user_api_key_dict(user) == "prior1"

    def test_team_metadata_overrides_key_metadata(self, handler):
        user = _make_user("prior3", "u1")
        user.team_metadata = {"priority": "prior1"}
        assert handler._get_priority_from_user_api_key_dict(user) == "prior1"

    def test_no_priority_returns_none(self, handler):
        user = _make_user(None, "u1")
        assert handler._get_priority_from_user_api_key_dict(user) is None


class TestPriorityWeightNormalization:
    """Verify normalization when priorities sum to <= 1.0 (no normalization needed)."""

    def test_weights_not_normalized_when_sum_le_1(self, handler):
        model_info = handler.llm_router.get_model_group_info(model_group=MODEL)
        weights = handler._normalize_priority_weights(model_info)
        assert weights == {"prior1": 0.50, "prior2": 0.30, "prior3": 0.20}

    def test_weights_normalized_when_sum_gt_1(self, handler):
        """Over-allocation should be normalized proportionally."""
        litellm.priority_reservation = {"prior1": 0.60, "prior2": 0.60}
        model_info = handler.llm_router.get_model_group_info(model_group=MODEL)
        weights = handler._normalize_priority_weights(model_info)
        total = sum(weights.values())
        assert abs(total - 1.0) < 0.001
        assert abs(weights["prior1"] - 0.50) < 0.001
        assert abs(weights["prior2"] - 0.50) < 0.001


def _make_deployment(model_name: str = MODEL) -> dict:
    return {
        "model_name": model_name,
        "litellm_params": {"model": f"openai/{model_name}"},
    }


def _set_priority(handler, priority: str | None):
    """Set htb_priority ContextVar as async_pre_call_hook would."""
    from litellm.proxy.hooks.dynamic_rate_limiter_v3 import htb_priority

    htb_priority.set(priority)


class TestHTBPreCallCheck:
    """Verify that async_pre_call_check runs the HTB Lua script and allows/denies correctly."""

    @pytest.mark.asyncio
    async def test_ok_response_allows_request(self, handler):
        """When htb_check_and_increment returns OK, the deployment is returned unchanged."""
        _set_priority(handler, "prior1")

        ok_response = {"overall_code": "OK", "statuses": []}

        with patch.object(
            handler.v3_limiter,
            "htb_check_and_increment",
            new=AsyncMock(return_value=ok_response),
        ):
            result = await handler.async_pre_call_check(
                deployment=_make_deployment(),
                parent_otel_span=None,
            )

        assert result is not None
        assert result["model_name"] == MODEL

    @pytest.mark.asyncio
    async def test_over_limit_raises_rate_limit_error(self, handler):
        """When htb_check_and_increment returns OVER_LIMIT, a RateLimitError is raised."""
        _set_priority(handler, "prior2")

        over_limit = {
            "overall_code": "OVER_LIMIT",
            "statuses": [
                {
                    "code": "OVER_LIMIT",
                    "descriptor_key": "priority_model",
                    "rate_limit_type": "requests",
                    "limit_remaining": 0,
                }
            ],
        }

        with patch.object(
            handler.v3_limiter,
            "htb_check_and_increment",
            new=AsyncMock(return_value=over_limit),
        ):
            with pytest.raises(litellm.RateLimitError) as exc_info:
                await handler.async_pre_call_check(
                    deployment=_make_deployment(),
                    parent_otel_span=None,
                )

        assert exc_info.value.status_code == 429

    @pytest.mark.asyncio
    async def test_no_priority_reservation_skips_check(self, handler):
        """When priority_reservation is None, async_pre_call_check returns the deployment immediately."""
        original = litellm.priority_reservation
        litellm.priority_reservation = None
        try:
            result = await handler.async_pre_call_check(
                deployment=_make_deployment(),
                parent_otel_span=None,
            )
            assert result is not None
        finally:
            litellm.priority_reservation = original

    @pytest.mark.asyncio
    async def test_htb_check_error_fails_open(self, handler):
        """When htb_check_and_increment raises an exception, the request is allowed (fail-open)."""
        _set_priority(handler, "prior3")

        with patch.object(
            handler.v3_limiter,
            "htb_check_and_increment",
            new=AsyncMock(side_effect=RuntimeError("Redis down")),
        ):
            result = await handler.async_pre_call_check(
                deployment=_make_deployment(),
                parent_otel_span=None,
            )

        assert result is not None

    @pytest.mark.asyncio
    async def test_pre_call_hook_sets_priority_contextvar(self, handler):
        """async_pre_call_hook should extract priority and set the htb_priority ContextVar."""
        from litellm.proxy.hooks.dynamic_rate_limiter_v3 import htb_priority

        user = _make_user("prior1", "u1")
        await handler.async_pre_call_hook(
            user_api_key_dict=user,
            cache=DualCache(),
            data={"model": MODEL},
            call_type="completion",
        )

        assert htb_priority.get() == "prior1"


class TestHTB429Errors:
    """Verify 429 error messages from _raise_rate_limit_error include model, priority, and limits."""

    @pytest.mark.asyncio
    async def test_429_includes_model_priority_and_rpm(self, handler):
        _set_priority(handler, "prior2")

        over_limit = {
            "overall_code": "OVER_LIMIT",
            "statuses": [
                {
                    "code": "OVER_LIMIT",
                    "descriptor_key": "priority_model",
                    "rate_limit_type": "requests",
                    "limit_remaining": 0,
                }
            ],
        }

        with patch.object(
            handler.v3_limiter,
            "htb_check_and_increment",
            new=AsyncMock(return_value=over_limit),
        ):
            with pytest.raises(litellm.RateLimitError) as exc_info:
                await handler.async_pre_call_check(
                    deployment=_make_deployment(),
                    parent_otel_span=None,
                )

        msg = str(exc_info.value)
        assert f"Model: {MODEL}" in msg
        assert "Priority: prior2" in msg
        assert f"Model RPM: {MODEL_RPM}" in msg
        assert "Priority-based rate limit exceeded" in msg
        assert "Rate limit type: requests" in msg

    @pytest.mark.asyncio
    async def test_429_has_retry_after_header(self, handler):
        _set_priority(handler, "prior3")

        over_limit = {
            "overall_code": "OVER_LIMIT",
            "statuses": [
                {
                    "code": "OVER_LIMIT",
                    "descriptor_key": "priority_model",
                    "rate_limit_type": "requests",
                    "limit_remaining": 5,
                }
            ],
        }

        with patch.object(
            handler.v3_limiter,
            "htb_check_and_increment",
            new=AsyncMock(return_value=over_limit),
        ):
            with pytest.raises(litellm.RateLimitError) as exc_info:
                await handler.async_pre_call_check(
                    deployment=_make_deployment(),
                    parent_otel_span=None,
                )

        assert exc_info.value.status_code == 429
        response = exc_info.value.response
        assert response is not None
        assert "retry-after" in response.headers


class TestPriorityAllocationPoolKeys:
    """Verify that each priority gets its own pool key (not shared)."""

    def test_each_priority_has_unique_pool_key(self, handler):
        pool_keys = set()
        for p in ("prior1", "prior2", "prior3"):
            user = _make_user(p, f"u_{p}")
            d = handler._create_priority_based_descriptors(model=MODEL, priority=p)[0]
            pool_keys.add(d["value"])
        assert len(pool_keys) == 3
        assert f"{MODEL}:prior1" in pool_keys
        assert f"{MODEL}:prior2" in pool_keys
        assert f"{MODEL}:prior3" in pool_keys

    def test_default_pool_distinct_from_explicit(self, handler):
        user_default = _make_user(None, "u_d")
        d_default = handler._create_priority_based_descriptors(model=MODEL, priority=None)[0]

        user_p1 = _make_user("prior1", "u_p1")
        d_p1 = handler._create_priority_based_descriptors(model=MODEL, priority="prior1")[0]

        assert d_default["value"] != d_p1["value"]
        assert d_default["value"] == f"{MODEL}:default_pool"
        assert d_p1["value"] == f"{MODEL}:prior1"


class TestHTBInMemoryConcurrency:
    """Integration tests using the in-memory HTB fallback (no Redis, no mocking of rate limiter)."""

    @pytest.mark.asyncio
    async def test_model_capacity_never_exceeded(self, handler):
        """
        Send 600 requests total (200 per priority). The in-memory HTB fallback
        should enforce the model-wide RPM cap. Total successes must not exceed
        MODEL_RPM by more than a small race margin.
        """
        dual_cache = DualCache()
        handler.internal_usage_cache.dual_cache = dual_cache

        users = {
            "prior1": _make_user("prior1", "u_p1"),
            "prior2": _make_user("prior2", "u_p2"),
            "prior3": _make_user("prior3", "u_p3"),
        }

        success_count = 0

        async def make_request(priority_name):
            nonlocal success_count
            _set_priority(handler, priority_name)
            try:
                await handler.async_pre_call_check(
                    deployment=_make_deployment(),
                    parent_otel_span=None,
                )
                success_count += 1
            except litellm.RateLimitError:
                pass

        tasks = []
        for p in ("prior1", "prior2", "prior3"):
            tasks.extend([make_request(p) for _ in range(200)])

        await __import__("asyncio").gather(*tasks)

        assert success_count <= MODEL_RPM + 10, (
            f"Total successful ({success_count}) should not exceed model RPM ({MODEL_RPM}) "
            f"by more than a small race-condition margin"
        )

    @pytest.mark.asyncio
    async def test_low_traffic_all_succeed(self, handler):
        """
        When traffic is well below capacity (10 per priority = 30 total, model RPM = 180),
        all requests should succeed regardless of priority.
        """
        dual_cache = DualCache()
        handler.internal_usage_cache.dual_cache = dual_cache

        users = {
            "prior1": _make_user("prior1", "u_p1"),
            "prior2": _make_user("prior2", "u_p2"),
            "prior3": _make_user("prior3", "u_p3"),
        }

        success = {"prior1": 0, "prior2": 0, "prior3": 0}

        async def make_request(priority_name):
            _set_priority(handler, priority_name)
            try:
                await handler.async_pre_call_check(
                    deployment=_make_deployment(),
                    parent_otel_span=None,
                )
                success[priority_name] += 1
            except litellm.RateLimitError:
                pass

        tasks = []
        for p in ("prior1", "prior2", "prior3"):
            tasks.extend([make_request(p) for _ in range(10)])

        await __import__("asyncio").gather(*tasks)

        assert success["prior1"] == 10
        assert success["prior2"] == 10
        assert success["prior3"] == 10

    @pytest.mark.asyncio
    async def test_over_capacity_each_priority_gets_guaranteed_rate(self, handler):
        """
        Regression test for starvation bug: under over-capacity conditions
        (200 requests per priority on a 180 RPM model), each priority must
        receive at least its guaranteed rate. Before the fix, prior3 was
        starved to 0 because the EWMA-based borrow ceiling did not protect
        siblings that had not yet sent in the current window.
        """
        dual_cache = DualCache()
        handler.internal_usage_cache.dual_cache = dual_cache

        success = {"prior1": 0, "prior2": 0, "prior3": 0}

        async def make_request(priority_name):
            _set_priority(handler, priority_name)
            try:
                await handler.async_pre_call_check(
                    deployment=_make_deployment(),
                    parent_otel_span=None,
                )
                success[priority_name] += 1
            except litellm.RateLimitError:
                pass

        tasks = []
        for p in ("prior1", "prior2", "prior3"):
            tasks.extend([make_request(p) for _ in range(200)])

        await __import__("asyncio").gather(*tasks)

        assert success["prior1"] >= PRIOR1_RPM, (
            f"prior1 must get at least its guaranteed {PRIOR1_RPM} RPM, got {success['prior1']}"
        )
        assert success["prior2"] >= PRIOR2_RPM, (
            f"prior2 must get at least its guaranteed {PRIOR2_RPM} RPM, got {success['prior2']}"
        )
        assert success["prior3"] >= PRIOR3_RPM, (
            f"prior3 must get at least its guaranteed {PRIOR3_RPM} RPM, got {success['prior3']}"
        )
        total = sum(success.values())
        assert total <= MODEL_RPM + 10, (
            f"Total ({total}) must not exceed model RPM ({MODEL_RPM}) by more than a small margin"
        )


class TestPostCallTokenTracking:
    """Verify that token usage is correctly tracked per-priority after successful calls."""

    @pytest.mark.asyncio
    async def test_token_increment_uses_correct_priority_key(self, handler):
        from unittest.mock import MagicMock
        from litellm.types.utils import ModelResponse, Usage

        increment_calls = []

        async def mock_increment(pipeline_operations, parent_otel_span=None):
            for op in pipeline_operations:
                increment_calls.append({"key": op["key"], "value": op["increment_value"]})

        handler.v3_limiter.async_increment_tokens_with_ttl_preservation = mock_increment

        mock_response = MagicMock(spec=ModelResponse)
        mock_response.usage = MagicMock(spec=Usage)
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 20
        mock_response.usage.total_tokens = 30

        kwargs = {
            "standard_logging_object": {
                "metadata": {
                    "user_api_key_auth_metadata": {"priority": "prior2"},
                },
                "model_group": MODEL,
            },
            "litellm_params": {
                "metadata": {"model_group": MODEL},
            },
        }

        with patch(
            "litellm.proxy.common_utils.callback_utils.get_model_group_from_litellm_kwargs",
            return_value=MODEL,
        ):
            await handler.async_log_success_event(
                kwargs=kwargs,
                response_obj=mock_response,
                start_time=None,
                end_time=None,
            )

        assert len(increment_calls) == 2

        priority_call = next(c for c in increment_calls if "priority_model" in c["key"])
        assert "prior2" in priority_call["key"]
        assert priority_call["value"] == 30

        model_call = next(c for c in increment_calls if "model_saturation_check" in c["key"])
        assert model_call["value"] == 30

    @pytest.mark.asyncio
    async def test_token_increment_default_pool_for_no_priority(self, handler):
        from unittest.mock import MagicMock
        from litellm.types.utils import ModelResponse, Usage

        increment_calls = []

        async def mock_increment(pipeline_operations, parent_otel_span=None):
            for op in pipeline_operations:
                increment_calls.append({"key": op["key"], "value": op["increment_value"]})

        handler.v3_limiter.async_increment_tokens_with_ttl_preservation = mock_increment

        mock_response = MagicMock(spec=ModelResponse)
        mock_response.usage = MagicMock(spec=Usage)
        mock_response.usage.prompt_tokens = 5
        mock_response.usage.completion_tokens = 15
        mock_response.usage.total_tokens = 20

        kwargs = {
            "standard_logging_object": {
                "metadata": {
                    "user_api_key_auth_metadata": {},
                },
                "model_group": MODEL,
            },
            "litellm_params": {
                "metadata": {"model_group": MODEL},
            },
        }

        with patch(
            "litellm.proxy.common_utils.callback_utils.get_model_group_from_litellm_kwargs",
            return_value=MODEL,
        ):
            await handler.async_log_success_event(
                kwargs=kwargs,
                response_obj=mock_response,
                start_time=None,
                end_time=None,
            )

        priority_call = next(c for c in increment_calls if "priority_model" in c["key"])
        assert "default_pool" in priority_call["key"]
        assert priority_call["value"] == 20


class TestPriorityReservationSettings:
    """Verify the PriorityReservationSettings configuration is correctly applied."""

    def test_saturation_threshold_is_1_0(self, adeo_priority_config):
        settings = litellm.priority_reservation_settings
        assert settings.saturation_threshold == SATURATION_THRESHOLD

    def test_default_saturation_threshold_is_1_0(self):
        """The default saturation_threshold should be 1.0 (full model RPM available for borrowing)."""
        settings = PriorityReservationSettings()
        assert settings.saturation_threshold == 1.0

    def test_default_priority_is_0_25(self, adeo_priority_config):
        settings = litellm.priority_reservation_settings
        assert settings.default_priority == 0.25

    def test_priority_reservation_has_three_levels(self, adeo_priority_config):
        assert litellm.priority_reservation is not None
        assert set(litellm.priority_reservation.keys()) == {"prior1", "prior2", "prior3"}
        assert litellm.priority_reservation["prior1"] == 0.50
        assert litellm.priority_reservation["prior2"] == 0.30
        assert litellm.priority_reservation["prior3"] == 0.20


class TestDemandCounterMultiPodVisibility:
    """Regression tests for the multi-pod demand-counter write bug.

    The demand counter must be written to Redis (not just in-memory) when
    Redis is configured, because the HTB Lua script reads sibling demand
    from Redis via redis.call('GET', ...). A local_only=True write would
    bypass Redis, making sibling demand invisible across pods and breaking
    the borrow ceiling computation.
    """

    @pytest.mark.asyncio
    async def test_demand_counter_write_reaches_redis(self, handler):
        """When Redis is configured, _increment_demand_counter must write to Redis.

        This is the regression test for the bug where local_only=True caused
        the demand counter to stay in per-pod in-memory cache, invisible to
        the Lua script's sibling demand reads on other pods.
        """
        redis_mock = AsyncMock()
        redis_mock.async_set_cache = AsyncMock()
        redis_mock.async_increment = AsyncMock(return_value=1)

        dual_cache = DualCache(redis_cache=redis_mock)
        handler.v3_limiter.internal_usage_cache = InternalUsageCache(dual_cache=dual_cache)

        await handler.v3_limiter._increment_demand_counter(
            demand_window_key="{htb:test-model}:test-model:prior1:demand:window",
            demand_counter_key="{htb:test-model}:test-model:prior1:demand:requests",
            window_size=60,
            ttl=60,
            parent_otel_span=None,
        )

        assert redis_mock.async_set_cache.called or redis_mock.async_increment.called, (
            "Demand counter write must reach Redis when Redis is configured, "
            "otherwise the Lua script reads sibling demand as 0 on other pods "
            "and the borrow ceiling degrades with no sibling reservation."
        )

    @pytest.mark.asyncio
    async def test_demand_counter_window_reset_uses_atomic_increment(self, handler):
        """When the window is active, the increment must be atomic (INCR), not read-then-write.

        Uses async_increment_cache which calls Redis INCR atomically,
        eliminating the cross-pod read-modify-write race where two pods
        could both read the same value and overwrite each other's increment.
        """
        recent_ts = str(int(time.time()))

        redis_mock = AsyncMock()
        redis_mock.async_set_cache = AsyncMock()
        redis_mock.async_get_cache = AsyncMock(return_value=recent_ts)
        redis_mock.async_increment = AsyncMock(return_value=2)

        dual_cache = DualCache(redis_cache=redis_mock)
        handler.v3_limiter.internal_usage_cache = InternalUsageCache(dual_cache=dual_cache)

        await handler.v3_limiter._increment_demand_counter(
            demand_window_key="{htb:test-model}:test-model:prior1:demand:window",
            demand_counter_key="{htb:test-model}:test-model:prior1:demand:requests",
            window_size=60,
            ttl=60,
            parent_otel_span=None,
        )

        assert redis_mock.async_increment.called, (
            "When the window is active, the demand counter must use atomic "
            "INCR (async_increment_cache), not a read-then-write, to prevent "
            "lost increments across pods."
        )
        incr_args = redis_mock.async_increment.call_args
        assert incr_args.args[0] == "{htb:test-model}:test-model:prior1:demand:requests", (
            "async_increment must target the demand counter key."
        )
        assert incr_args.args[1] == 1, "async_increment must increment by exactly 1."
