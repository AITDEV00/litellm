"""
Tests for ADEO 3-priority reservation configuration.

Production config (litellm-proxy.yaml):
    priority_reservation:
      prior1: 0.50
      prior2: 0.30
      prior3: 0.20
    priority_reservation_settings:
      saturation_threshold: 0.80

Model: Qwen/Qwen3.5-0.8B with rpm=180

These tests verify that the priority rules are respected:
- Descriptor allocation matches 50/30/20 split
- Under-saturation (< 80%): generous mode, all priorities can borrow
- Over-saturation (>= 80%): strict mode, each priority capped at its reservation
- Model capacity is never exceeded (100% hard cap)
- Priority ordering: prior1 > prior2 > prior3 when saturated
- Default-priority keys share a single pool
- 429 error messages include model name and configured limits
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.abspath("../../../.."))

import litellm
from litellm import DualCache, Router
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.hooks.dynamic_rate_limiter_v3 import (
    _PROXY_DynamicRateLimitHandlerV3 as DynamicRateLimitHandler,
)
from litellm.types.utils import PriorityReservationSettings

MODEL = "Qwen/Qwen3.5-0.8B"
MODEL_RPM = 180
PRIOR1_RPM = 90   # 0.50 * 180
PRIOR2_RPM = 54   # 0.30 * 180
PRIOR3_RPM = 36   # 0.20 * 180
SATURATION_THRESHOLD = 0.80


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
        descs = handler._create_priority_based_descriptors(
            model=MODEL, user_api_key_dict=user, priority="prior1"
        )
        assert len(descs) == 1
        d = descs[0]
        assert d["value"] == f"{MODEL}:prior1"
        assert d["rate_limit"]["requests_per_unit"] == PRIOR1_RPM
        assert d["key"] == "priority_model"

    def test_prior2_gets_30_percent(self, handler):
        user = _make_user("prior2", "u2")
        descs = handler._create_priority_based_descriptors(
            model=MODEL, user_api_key_dict=user, priority="prior2"
        )
        assert len(descs) == 1
        d = descs[0]
        assert d["value"] == f"{MODEL}:prior2"
        assert d["rate_limit"]["requests_per_unit"] == PRIOR2_RPM

    def test_prior3_gets_20_percent(self, handler):
        user = _make_user("prior3", "u3")
        descs = handler._create_priority_based_descriptors(
            model=MODEL, user_api_key_dict=user, priority="prior3"
        )
        assert len(descs) == 1
        d = descs[0]
        assert d["value"] == f"{MODEL}:prior3"
        assert d["rate_limit"]["requests_per_unit"] == PRIOR3_RPM

    def test_ratio_is_5_3_2(self, handler):
        """The three allocations should be in exact 5:3:2 ratio."""
        descs = {}
        for p in ("prior1", "prior2", "prior3"):
            user = _make_user(p, f"u_{p}")
            d = handler._create_priority_based_descriptors(
                model=MODEL, user_api_key_dict=user, priority=p
            )[0]
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
        descs = handler._create_priority_based_descriptors(
            model=MODEL, user_api_key_dict=user, priority=None
        )
        assert len(descs) == 1
        d = descs[0]
        assert d["value"] == f"{MODEL}:default_pool"
        assert d["rate_limit"]["requests_per_unit"] == int(MODEL_RPM * 0.25)

    def test_multiple_default_keys_share_one_pool(self, handler):
        """All default-priority keys must share the same pool key, not get individual pools."""
        users = [_make_user(None, f"u{i}") for i in range(5)]
        pool_keys = set()
        for u in users:
            d = handler._create_priority_based_descriptors(
                model=MODEL, user_api_key_dict=u, priority=None
            )[0]
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


class TestSaturationEnforcement:
    """Verify that priority enforcement only kicks in above saturation_threshold."""

    @pytest.mark.asyncio
    async def test_under_saturation_does_not_enforce_priority(self, handler):
        """
        When saturation < 0.80, priority descriptors are tracked but NOT enforced.
        Only the model-wide (100% capacity) descriptor is enforced.
        """
        user = _make_user("prior1", "u1")

        captured = {}

        async def fake_atomic(descriptors, increments, parent_otel_span=None):
            captured["enforced_descriptors"] = descriptors
            return {"overall_code": "OK", "statuses": []}

        async def fake_should_rate_limit(descriptors, parent_otel_span=None, read_only=False):
            captured["tracked_descriptors"] = descriptors
            return {"overall_code": "OK", "statuses": []}

        with patch.object(
            handler.v3_limiter, "atomic_check_and_increment_by_n", side_effect=fake_atomic
        ), patch.object(
            handler.v3_limiter, "should_rate_limit", side_effect=fake_should_rate_limit
        ), patch.object(
            handler, "_check_model_saturation", return_value=0.50
        ):
            await handler.async_pre_call_hook(
                user_api_key_dict=user,
                cache=DualCache(),
                data={"model": MODEL},
                call_type="completion",
            )

        enforced_keys = [d["key"] for d in captured["enforced_descriptors"]]
        assert "model_saturation_check" in enforced_keys
        assert "priority_model" not in enforced_keys

        tracked_keys = [d["key"] for d in captured.get("tracked_descriptors", [])]
        assert "priority_model" in tracked_keys

    @pytest.mark.asyncio
    async def test_over_saturation_enforces_priority(self, handler):
        """
        When saturation >= 0.80, both model-wide AND priority descriptors are enforced.
        """
        user = _make_user("prior2", "u2")

        captured = {}

        async def fake_atomic(descriptors, increments, parent_otel_span=None):
            captured["enforced_descriptors"] = descriptors
            return {"overall_code": "OK", "statuses": []}

        with patch.object(
            handler.v3_limiter, "atomic_check_and_increment_by_n", side_effect=fake_atomic
        ), patch.object(
            handler, "_check_model_saturation", return_value=0.85
        ):
            await handler.async_pre_call_hook(
                user_api_key_dict=user,
                cache=DualCache(),
                data={"model": MODEL},
                call_type="completion",
            )

        enforced_keys = [d["key"] for d in captured["enforced_descriptors"]]
        assert "model_saturation_check" in enforced_keys
        assert "priority_model" in enforced_keys

    @pytest.mark.asyncio
    async def test_at_threshold_enforces_priority(self, handler):
        """Saturation exactly at threshold (0.80) should enforce priority (>= comparison)."""
        user = _make_user("prior1", "u1")

        captured = {}

        async def fake_atomic(descriptors, increments, parent_otel_span=None):
            captured["enforced_descriptors"] = descriptors
            return {"overall_code": "OK", "statuses": []}

        with patch.object(
            handler.v3_limiter, "atomic_check_and_increment_by_n", side_effect=fake_atomic
        ), patch.object(
            handler, "_check_model_saturation", return_value=0.80
        ):
            await handler.async_pre_call_hook(
                user_api_key_dict=user,
                cache=DualCache(),
                data={"model": MODEL},
                call_type="completion",
            )

        enforced_keys = [d["key"] for d in captured["enforced_descriptors"]]
        assert "priority_model" in enforced_keys


class TestModelCapacityEnforced:
    """Verify that the model-wide 100% capacity limit is always enforced."""

    @pytest.mark.asyncio
    async def test_model_capacity_always_enforced(self, handler):
        """Even under saturation, the model_saturation_check descriptor is always in the enforced set."""
        user = _make_user("prior3", "u3")

        captured = {}

        async def fake_atomic(descriptors, increments, parent_otel_span=None):
            captured["enforced_descriptors"] = descriptors
            return {"overall_code": "OK", "statuses": []}

        with patch.object(
            handler.v3_limiter, "atomic_check_and_increment_by_n", side_effect=fake_atomic
        ), patch.object(
            handler, "_check_model_saturation", return_value=0.95
        ):
            await handler.async_pre_call_hook(
                user_api_key_dict=user,
                cache=DualCache(),
                data={"model": MODEL},
                call_type="completion",
            )

        enforced = captured["enforced_descriptors"]
        model_desc = [d for d in enforced if d["key"] == "model_saturation_check"]
        assert len(model_desc) == 1
        assert model_desc[0]["rate_limit"]["requests_per_unit"] == MODEL_RPM

    @pytest.mark.asyncio
    async def test_model_capacity_429_blocks_request(self, handler):
        """When model capacity is exceeded, a 429 is raised regardless of priority."""
        user = _make_user("prior1", "u1")

        over_limit = {
            "overall_code": "OVER_LIMIT",
            "statuses": [
                {
                    "code": "OVER_LIMIT",
                    "descriptor_key": "model_saturation_check",
                    "rate_limit_type": "requests",
                    "limit_remaining": 0,
                }
            ],
        }

        with patch.object(
            handler.v3_limiter,
            "atomic_check_and_increment_by_n",
            new=AsyncMock(return_value=over_limit),
        ), patch.object(
            handler, "_check_model_saturation", return_value=0.95
        ):
            with pytest.raises(Exception) as exc_info:
                await handler.async_pre_call_hook(
                    user_api_key_dict=user,
                    cache=DualCache(),
                    data={"model": MODEL},
                    call_type="completion",
                )

        assert exc_info.value.status_code == 429
        assert "Model capacity reached" in str(exc_info.value.detail)


class TestPriorityBased429:
    """Verify 429 error messages include model name, priority, and configured limits."""

    @pytest.mark.asyncio
    async def test_priority_429_includes_model_and_limits(self, handler):
        user = _make_user("prior2", "u2")
        model_info = handler.llm_router.get_model_group_info(model_group=MODEL)

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
            "atomic_check_and_increment_by_n",
            new=AsyncMock(return_value=over_limit),
        ):
            with pytest.raises(Exception) as exc_info:
                await handler._check_rate_limits(
                    model=MODEL,
                    model_group_info=model_info,
                    user_api_key_dict=user,
                    priority="prior2",
                    saturation=0.90,
                    data={"model": MODEL},
                )

        assert exc_info.value.status_code == 429
        detail = exc_info.value.detail
        error_msg = detail["error"]
        assert f"Model: {MODEL}" in error_msg
        assert "Priority: prior2" in error_msg
        assert f"Model RPM: {MODEL_RPM}" in error_msg
        assert "Priority-based rate limit exceeded" in error_msg
        assert "Model saturation:" in error_msg

    @pytest.mark.asyncio
    async def test_priority_429_has_saturation_header(self, handler):
        user = _make_user("prior3", "u3")
        model_info = handler.llm_router.get_model_group_info(model_group=MODEL)

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
            "atomic_check_and_increment_by_n",
            new=AsyncMock(return_value=over_limit),
        ):
            with pytest.raises(Exception) as exc_info:
                await handler._check_rate_limits(
                    model=MODEL,
                    model_group_info=model_info,
                    user_api_key_dict=user,
                    priority="prior3",
                    saturation=0.92,
                    data={"model": MODEL},
                )

        headers = exc_info.value.headers
        assert headers["x-litellm-priority"] == "prior3"
        assert "x-litellm-saturation" in headers


class TestPriorityAllocationPoolKeys:
    """Verify that each priority gets its own pool key (not shared)."""

    def test_each_priority_has_unique_pool_key(self, handler):
        pool_keys = set()
        for p in ("prior1", "prior2", "prior3"):
            user = _make_user(p, f"u_{p}")
            d = handler._create_priority_based_descriptors(
                model=MODEL, user_api_key_dict=user, priority=p
            )[0]
            pool_keys.add(d["value"])
        assert len(pool_keys) == 3
        assert f"{MODEL}:prior1" in pool_keys
        assert f"{MODEL}:prior2" in pool_keys
        assert f"{MODEL}:prior3" in pool_keys

    def test_default_pool_distinct_from_explicit(self, handler):
        user_default = _make_user(None, "u_d")
        d_default = handler._create_priority_based_descriptors(
            model=MODEL, user_api_key_dict=user_default, priority=None
        )[0]

        user_p1 = _make_user("prior1", "u_p1")
        d_p1 = handler._create_priority_based_descriptors(
            model=MODEL, user_api_key_dict=user_p1, priority="prior1"
        )[0]

        assert d_default["value"] != d_p1["value"]
        assert d_default["value"] == f"{MODEL}:default_pool"
        assert d_p1["value"] == f"{MODEL}:prior1"


class TestConcurrentPriorityRequests:
    """Integration-style tests with actual DualCache counters (no mocking of rate limiter)."""

    @pytest.mark.asyncio
    async def test_priorities_get_proportional_throughput_when_saturated(self, handler):
        """
        Send 200 requests from each priority (600 total, far over 180 RPM capacity).
        With saturation forced high, strict mode is always on.

        prior1 should get roughly 90 (50%), prior2 ~54 (30%), prior3 ~36 (20%).
        The ordering prior1 > prior2 > prior3 must hold, and prior1's
        share of successful requests should be close to 50%.
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
            try:
                await handler.async_pre_call_hook(
                    user_api_key_dict=users[priority_name],
                    cache=dual_cache,
                    data={"model": MODEL},
                    call_type="completion",
                )
                success[priority_name] += 1
            except Exception:
                pass

        tasks = []
        for p in ("prior1", "prior2", "prior3"):
            tasks.extend([make_request(p) for _ in range(200)])

        with patch.object(handler, "_check_model_saturation", return_value=0.95):
            await __import__("asyncio").gather(*tasks)

        total = sum(success.values())

        assert success["prior1"] > success["prior2"], (
            f"prior1 ({success['prior1']}) should exceed prior2 ({success['prior2']})"
        )
        assert success["prior2"] > success["prior3"], (
            f"prior2 ({success['prior2']}) should exceed prior3 ({success['prior3']})"
        )

        if total > 0:
            p1_share = success["prior1"] / total
            assert 0.35 < p1_share < 0.65, (
                f"prior1 share should be near 50%, got {p1_share:.1%}"
            )

    @pytest.mark.asyncio
    async def test_model_capacity_never_exceeded(self, handler):
        """
        Send 500 requests total. The sum of all successful requests should
        never exceed the model's RPM capacity (180).
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
            try:
                await handler.async_pre_call_hook(
                    user_api_key_dict=users[priority_name],
                    cache=dual_cache,
                    data={"model": MODEL},
                    call_type="completion",
                )
                success_count += 1
            except Exception:
                pass

        tasks = []
        for p in ("prior1", "prior2", "prior3"):
            tasks.extend([make_request(p) for _ in range(200)])

        with patch.object(handler, "_check_model_saturation", return_value=0.95):
            await __import__("asyncio").gather(*tasks)

        assert success_count <= MODEL_RPM + 10, (
            f"Total successful ({success_count}) should not exceed model RPM ({MODEL_RPM}) "
            f"by more than a small race-condition margin"
        )

    @pytest.mark.asyncio
    async def test_low_traffic_all_succeed_under_saturation(self, handler):
        """
        When traffic is well below capacity, all requests should succeed
        regardless of priority (generous mode).
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
            try:
                await handler.async_pre_call_hook(
                    user_api_key_dict=users[priority_name],
                    cache=dual_cache,
                    data={"model": MODEL},
                    call_type="completion",
                )
                success[priority_name] += 1
            except Exception:
                pass

        tasks = []
        for p in ("prior1", "prior2", "prior3"):
            tasks.extend([make_request(p) for _ in range(10)])

        await __import__("asyncio").gather(*tasks)

        assert success["prior1"] == 10
        assert success["prior2"] == 10
        assert success["prior3"] == 10


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

    def test_saturation_threshold_is_0_80(self, adeo_priority_config):
        settings = litellm.priority_reservation_settings
        assert settings.saturation_threshold == SATURATION_THRESHOLD

    def test_default_priority_is_0_25(self, adeo_priority_config):
        settings = litellm.priority_reservation_settings
        assert settings.default_priority == 0.25

    def test_priority_reservation_has_three_levels(self, adeo_priority_config):
        assert litellm.priority_reservation is not None
        assert set(litellm.priority_reservation.keys()) == {"prior1", "prior2", "prior3"}
        assert litellm.priority_reservation["prior1"] == 0.50
        assert litellm.priority_reservation["prior2"] == 0.30
        assert litellm.priority_reservation["prior3"] == 0.20
