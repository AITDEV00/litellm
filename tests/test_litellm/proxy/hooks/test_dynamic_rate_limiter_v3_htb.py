"""
Test priority-based rate limiting for dynamic_rate_limiter_v3.

Core tests to validate that priority weights are respected (0.9/0.1) instead of equal splitting (0.5/0.5).
"""

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.abspath("../../../.."))

import litellm
from litellm import DualCache, Router
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.hooks.dynamic_rate_limiter_v3_htb import (
    _PROXY_DynamicRateLimitHandlerV3Htb as DynamicRateLimitHandler,
    htb_priority,
)


class TimeController:
    def __init__(self):
        self._current = datetime.utcnow()

    def now(self) -> datetime:
        return self._current

    def advance(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)


@pytest.fixture
def time_controller(monkeypatch):
    controller = TimeController()
    monkeypatch.setattr(time, "time", lambda: controller.now().timestamp())
    return controller


@pytest.mark.asyncio
async def test_priority_weight_allocation():
    """
    Test that priority weights are correctly applied instead of equal splitting.

    With priority_reservation = {"high": 0.9, "low": 0.1}:
    - High priority should get 90% of TPM (900 out of 1000)
    - Low priority should get 10% of TPM (100 out of 1000)

    This validates the core fix where before it would split 50/50.
    """
    # Set up environment for premium feature
    os.environ["LITELLM_LICENSE"] = "test-license-key"

    # Set up priority reservations
    litellm.priority_reservation = {"high": 0.9, "low": 0.1}

    dual_cache = DualCache()
    handler = DynamicRateLimitHandler(internal_usage_cache=dual_cache)

    model = "test-model"
    total_tpm = 1000

    llm_router = Router(
        model_list=[
            {
                "model_name": model,
                "litellm_params": {
                    "model": "gpt-3.5-turbo",
                    "api_key": "test-key",
                    "api_base": "test-base",
                    "tpm": total_tpm,
                },
            }
        ]
    )
    handler.update_variables(llm_router=llm_router)

    # Test high priority allocation
    high_priority_user = UserAPIKeyAuth()
    high_priority_user.metadata = {"priority": "high"}

    high_descriptors = handler._create_priority_based_descriptors(
        model=model,
        priority="high",
    )

    assert len(high_descriptors) == 1
    high_descriptor = high_descriptors[0]
    expected_high_tpm = int(total_tpm * 0.9)  # 900
    actual_high_tpm = high_descriptor["rate_limit"]["tokens_per_unit"]

    assert actual_high_tpm == expected_high_tpm, (
        f"High priority should get {expected_high_tpm} TPM (90%), got {actual_high_tpm}"
    )
    assert high_descriptor["value"] == f"{model}:high"

    # Test low priority allocation
    low_priority_user = UserAPIKeyAuth()
    low_priority_user.metadata = {"priority": "low"}

    low_descriptors = handler._create_priority_based_descriptors(
        model=model,
        priority="low",
    )

    assert len(low_descriptors) == 1
    low_descriptor = low_descriptors[0]
    expected_low_tpm = int(total_tpm * 0.1)  # 100
    actual_low_tpm = low_descriptor["rate_limit"]["tokens_per_unit"]

    assert actual_low_tpm == expected_low_tpm, (
        f"Low priority should get {expected_low_tpm} TPM (10%), got {actual_low_tpm}"
    )
    assert low_descriptor["value"] == f"{model}:low"

    # Verify the ratio is 9:1, not 1:1 (equal splitting)
    ratio = actual_high_tpm / actual_low_tpm
    expected_ratio = 9.0
    assert abs(ratio - expected_ratio) < 0.1, f"High:Low ratio should be {expected_ratio}:1, got {ratio}:1"


@pytest.mark.asyncio
async def test_concurrent_priority_requests():
    """
    Test the core issue: 5 concurrent requests with different priorities should get
    proper allocation based on priority weights, not equal splitting.

    This tests the exact scenario mentioned: priorities 0.9 and 0.1 should be 0.9/0.1, not 0.5/0.5.
    """
    # Set up environment for premium feature
    os.environ["LITELLM_LICENSE"] = "test-license-key"

    # Set up the exact scenario from the issue
    litellm.priority_reservation = {"high": 0.9, "low": 0.1}

    dual_cache = DualCache()
    handler = DynamicRateLimitHandler(internal_usage_cache=dual_cache)

    model = "test-model"
    total_tpm = 1000

    llm_router = Router(
        model_list=[
            {
                "model_name": model,
                "litellm_params": {
                    "model": "gpt-3.5-turbo",
                    "api_key": "test-key",
                    "api_base": "test-base",
                    "tpm": total_tpm,
                },
            }
        ]
    )
    handler.update_variables(llm_router=llm_router)

    # Create 5 concurrent users - 3 high priority, 2 low priority
    high_priority_users = []
    low_priority_users = []

    for i in range(3):  # 3 high priority users
        user = UserAPIKeyAuth()
        user.metadata = {"priority": "high"}
        user.user_id = f"high_user_{i}"
        high_priority_users.append(user)

    for i in range(2):  # 2 low priority users
        user = UserAPIKeyAuth()
        user.metadata = {"priority": "low"}
        user.user_id = f"low_user_{i}"
        low_priority_users.append(user)

    # Test all high priority users get the same allocation (not divided)
    for user in high_priority_users:
        descriptors = handler._create_priority_based_descriptors(
            model=model,
            priority="high",
        )

        assert len(descriptors) == 1
        descriptor = descriptors[0]
        # Each high priority user should get 900 TPM, not divided by 3
        assert descriptor["rate_limit"]["tokens_per_unit"] == 900, (
            f"High priority user {user.user_id} should get 900 TPM, got {descriptor['rate_limit']['tokens_per_unit']}"
        )
        assert descriptor["value"] == f"{model}:high"

    # Test all low priority users get the same allocation (not divided)
    for user in low_priority_users:
        descriptors = handler._create_priority_based_descriptors(
            model=model,
            priority="low",
        )

        assert len(descriptors) == 1
        descriptor = descriptors[0]
        # Each low priority user should get 100 TPM, not divided by 2
        assert descriptor["rate_limit"]["tokens_per_unit"] == 100, (
            f"Low priority user {user.user_id} should get 100 TPM, got {descriptor['rate_limit']['tokens_per_unit']}"
        )
        assert descriptor["value"] == f"{model}:low"


@pytest.mark.asyncio
async def test_100_concurrent_priority_requests(time_controller):
    """
    Stress test: 100 concurrent requests with mixed priorities over 10 seconds.

    This validates that the priority system works correctly under high load:
    - 70 high priority requests (should get 900 TPM each)
    - 30 low priority requests (should get 100 TPM each)
    - Spread across 10 seconds to simulate real-world load
    """
    # Set up environment for premium feature
    os.environ["LITELLM_LICENSE"] = "test-license-key"

    # Set up priority reservations
    litellm.priority_reservation = {"high": 0.9, "low": 0.1}

    dual_cache = DualCache()
    handler = DynamicRateLimitHandler(internal_usage_cache=dual_cache, time_provider=time_controller.now)

    model = "stress-test-model"
    total_tpm = 1000

    llm_router = Router(
        model_list=[
            {
                "model_name": model,
                "litellm_params": {
                    "model": "gpt-3.5-turbo",
                    "api_key": "test-key",
                    "api_base": "test-base",
                    "tpm": total_tpm,
                    "rpm": 500,  # Also test RPM limits
                },
            }
        ]
    )
    handler.update_variables(llm_router=llm_router)

    # Create 100 users: 70 high priority, 30 low priority
    all_users = []

    # 70 high priority users
    for i in range(70):
        user = UserAPIKeyAuth()
        user.metadata = {"priority": "high"}
        user.user_id = f"high_stress_user_{i}"
        all_users.append((user, "high", 900, 450))  # expected TPM, expected RPM

    # 30 low priority users
    for i in range(30):
        user = UserAPIKeyAuth()
        user.metadata = {"priority": "low"}
        user.user_id = f"low_stress_user_{i}"
        all_users.append((user, "low", 100, 50))  # expected TPM, expected RPM

    async def test_user_descriptors(user_data):
        """Test descriptor creation for a single user."""
        user, priority, expected_tpm, expected_rpm = user_data

        descriptors = handler._create_priority_based_descriptors(
            model=model,
            priority=priority,
        )

        assert len(descriptors) == 1, f"User {user.user_id} should have exactly 1 descriptor"
        descriptor = descriptors[0]

        # Validate TPM allocation
        actual_tpm = descriptor["rate_limit"]["tokens_per_unit"]
        assert actual_tpm == expected_tpm, (
            f"User {user.user_id} ({priority}) should get {expected_tpm} TPM, got {actual_tpm}"
        )

        # Validate RPM allocation
        actual_rpm = descriptor["rate_limit"]["requests_per_unit"]
        assert actual_rpm == expected_rpm, (
            f"User {user.user_id} ({priority}) should get {expected_rpm} RPM, got {actual_rpm}"
        )

        # Validate descriptor key
        assert descriptor["value"] == f"{model}:{priority}"
        assert descriptor["key"] == "priority_model"

        return {
            "user_id": user.user_id,
            "priority": priority,
            "tpm": actual_tpm,
            "rpm": actual_rpm,
            "success": True,
        }

    # Run all 100 requests concurrently to simulate high load
    start_time = time.time()

    # Split into batches to simulate requests over 10 seconds
    batch_size = 10  # 10 requests per batch
    batches = [all_users[i : i + batch_size] for i in range(0, len(all_users), batch_size)]

    all_results = []

    for batch_idx, batch in enumerate(batches):
        # Process each batch concurrently
        batch_tasks = [test_user_descriptors(user_data) for user_data in batch]
        batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
        all_results.extend(batch_results)

        # Add small delay between batches to spread over ~10 seconds
        if batch_idx < len(batches) - 1:  # Don't sleep after last batch
            await asyncio.sleep(0)
            time_controller.advance(1.0)  # simulate 1s passing between batches

    end_time = time.time()
    total_duration = end_time - start_time

    # Validate that the test ran over approximately 10 seconds
    assert total_duration >= 9.0, f"Test should take ~10 seconds, took {total_duration:.2f}s"
    assert total_duration <= 15.0, f"Test took too long: {total_duration:.2f}s"

    # Validate all requests were successful
    successful_results = [r for r in all_results if isinstance(r, dict) and r.get("success")]
    assert len(successful_results) == 100, f"Expected 100 successful results, got {len(successful_results)}"

    # Validate priority distribution
    high_priority_results = [r for r in successful_results if r["priority"] == "high"]
    low_priority_results = [r for r in successful_results if r["priority"] == "low"]

    assert len(high_priority_results) == 70, f"Expected 70 high priority results, got {len(high_priority_results)}"
    assert len(low_priority_results) == 30, f"Expected 30 low priority results, got {len(low_priority_results)}"

    # Validate all high priority users got correct allocation
    for result in high_priority_results:
        assert result["tpm"] == 900, f"High priority user {result['user_id']} got {result['tpm']} TPM, expected 900"
        assert result["rpm"] == 450, f"High priority user {result['user_id']} got {result['rpm']} RPM, expected 450"

    # Validate all low priority users got correct allocation
    for result in low_priority_results:
        assert result["tpm"] == 100, f"Low priority user {result['user_id']} got {result['tpm']} TPM, expected 100"
        assert result["rpm"] == 50, f"Low priority user {result['user_id']} got {result['rpm']} RPM, expected 50"

    print(f"✅ Successfully processed 100 concurrent requests in {total_duration:.2f}s")
    print(f"   - 70 high priority users: 900 TPM, 450 RPM each")
    print(f"   - 30 low priority users: 100 TPM, 50 RPM each")
    print(f"   - Priority ratio maintained: 9:1 (TPM) and 9:1 (RPM)")


@pytest.mark.asyncio
async def test_default_priority_shared_pool():
    """
    Test that keys without explicit priority share ONE default pool, not get individual allocations.

    With default_priority=0.25:
    - Key A, B, C (no priority) should share ONE 25 RPM pool
    - NOT get 25 RPM each (which would be 75 RPM total)
    """
    os.environ["LITELLM_LICENSE"] = "test-license-key"

    litellm.priority_reservation = {"prod": 0.75}
    litellm.priority_reservation_settings.default_priority = 0.25

    dual_cache = DualCache()
    handler = DynamicRateLimitHandler(internal_usage_cache=dual_cache)

    model = "test-default-pool"
    total_rpm = 100

    llm_router = Router(
        model_list=[
            {
                "model_name": model,
                "litellm_params": {
                    "model": "gpt-3.5-turbo",
                    "api_key": "test-key",
                    "api_base": "test-base",
                    "rpm": total_rpm,
                },
            }
        ]
    )
    handler.update_variables(llm_router=llm_router)

    # Create 3 users without explicit priority
    user_a = UserAPIKeyAuth()
    user_a.metadata = {}
    user_a.user_id = "user_a"

    user_b = UserAPIKeyAuth()
    user_b.metadata = {}
    user_b.user_id = "user_b"

    user_c = UserAPIKeyAuth()
    user_c.metadata = {}
    user_c.user_id = "user_c"

    # Get descriptors for each
    desc_a = handler._create_priority_based_descriptors(model=model, priority=None)
    desc_b = handler._create_priority_based_descriptors(model=model, priority=None)
    desc_c = handler._create_priority_based_descriptors(model=model, priority=None)

    # All should use the SAME shared pool key
    assert desc_a[0]["value"] == f"{model}:default_pool"
    assert desc_b[0]["value"] == f"{model}:default_pool"
    assert desc_c[0]["value"] == f"{model}:default_pool"

    # All should have same limit (25 RPM SHARED, not 25 RPM each)
    assert desc_a[0]["rate_limit"]["requests_per_unit"] == 25
    assert desc_b[0]["rate_limit"]["requests_per_unit"] == 25
    assert desc_c[0]["rate_limit"]["requests_per_unit"] == 25

    # Verify explicit priority uses different pool
    user_prod = UserAPIKeyAuth()
    user_prod.metadata = {"priority": "prod"}
    desc_prod = handler._create_priority_based_descriptors(model=model, priority="prod")

    assert desc_prod[0]["value"] == f"{model}:prod"
    assert desc_prod[0]["rate_limit"]["requests_per_unit"] == 75
    assert desc_prod[0]["value"] != desc_a[0]["value"]  # Different pools

    print("✅ Default priority test passed:")
    print(f"   - 3 keys without priority share ONE pool: {desc_a[0]['value']}")
    print(f"   - Shared pool limit: {desc_a[0]['rate_limit']['requests_per_unit']} RPM")
    print(f"   - Explicit priority 'prod' uses separate pool: {desc_prod[0]['value']}")


@pytest.mark.asyncio
async def test_async_log_success_event_increments_by_actual_tokens():
    """
    Test that async_log_success_event increments token counters by actual token usage.

    This validates the fix for Bug 1: Token count was incrementing by 1 instead of actual usage.
    The async_log_success_event should increment both model_saturation_check and priority_model
    counters by the actual completion_tokens (when rate_limit_type=output).
    """
    from unittest.mock import MagicMock

    from litellm.types.utils import ModelResponse, Usage

    os.environ["LITELLM_LICENSE"] = "test-license-key"
    litellm.priority_reservation = {"dev": 0.1, "prod": 0.9}

    dual_cache = DualCache()
    handler = DynamicRateLimitHandler(internal_usage_cache=dual_cache)

    model = "test-token-increment"
    llm_router = Router(
        model_list=[
            {
                "model_name": model,
                "litellm_params": {
                    "model": "gpt-3.5-turbo",
                    "api_key": "test-key",
                    "api_base": "test-base",
                    "tpm": 1000,
                },
            }
        ]
    )
    handler.update_variables(llm_router=llm_router)

    # Track what gets incremented
    increment_calls = []

    async def mock_increment(pipeline_operations, parent_otel_span=None):
        for op in pipeline_operations:
            increment_calls.append(
                {
                    "key": op["key"],
                    "increment_value": op["increment_value"],
                }
            )

    handler.v3_limiter.async_increment_tokens_with_ttl_preservation = mock_increment

    # Create mock response with 50 completion tokens
    mock_response = MagicMock(spec=ModelResponse)
    mock_response.usage = MagicMock(spec=Usage)
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 50
    mock_response.usage.total_tokens = 60

    # Create kwargs with priority in user_api_key_auth_metadata
    kwargs = {
        "standard_logging_object": {
            "metadata": {
                "user_api_key_auth_metadata": {"priority": "dev"},
            },
            "model_group": model,
        },
        "litellm_params": {
            "metadata": {"model_group": model},
        },
    }

    with patch(
        "litellm.proxy.common_utils.callback_utils.get_model_group_from_litellm_kwargs",
        return_value=model,
    ):
        await handler.async_log_success_event(
            kwargs=kwargs,
            response_obj=mock_response,
            start_time=None,
            end_time=None,
        )

    # Verify increments happened with actual token count (60 total tokens)
    assert len(increment_calls) == 2, f"Expected 2 increment calls, got {len(increment_calls)}"

    # Both should increment by 50 (total_tokens, since rate_limit_type defaults to 'total')
    for call in increment_calls:
        assert call["increment_value"] == 60, (
            f"Expected increment of 60 tokens, got {call['increment_value']} for key {call['key']}"
        )

    # Verify correct keys were used
    keys = [call["key"] for call in increment_calls]
    assert any("model_saturation_check" in k for k in keys), "Should increment model_saturation_check"
    assert any("priority_model" in k and "dev" in k for k in keys), (
        "Should increment priority_model with 'dev' priority"
    )


@pytest.mark.asyncio
async def test_async_log_success_event_uses_team_priority_from_auth_metadata():
    """
    Test that async_log_success_event correctly retrieves priority from user_api_key_auth_metadata.

    This validates the fix where priority is retrieved from standard_logging_metadata.user_api_key_auth_metadata
    instead of just standard_logging_metadata.priority. This is important for team-based priority inheritance.
    """
    from unittest.mock import MagicMock

    from litellm.types.utils import ModelResponse, Usage

    os.environ["LITELLM_LICENSE"] = "test-license-key"
    litellm.priority_reservation = {"team_priority": 0.8, "default": 0.2}

    dual_cache = DualCache()
    handler = DynamicRateLimitHandler(internal_usage_cache=dual_cache)

    model = "test-team-priority"
    llm_router = Router(
        model_list=[
            {
                "model_name": model,
                "litellm_params": {
                    "model": "gpt-3.5-turbo",
                    "api_key": "test-key",
                    "api_base": "test-base",
                    "tpm": 1000,
                },
            }
        ]
    )
    handler.update_variables(llm_router=llm_router)

    # Track incremented keys to verify priority is used correctly
    incremented_keys = []

    async def mock_increment(pipeline_operations, parent_otel_span=None):
        for op in pipeline_operations:
            incremented_keys.append(op["key"])

    handler.v3_limiter.async_increment_tokens_with_ttl_preservation = mock_increment

    # Create mock response
    mock_response = MagicMock(spec=ModelResponse)
    mock_response.usage = MagicMock(spec=Usage)
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 20
    mock_response.usage.total_tokens = 30

    # Simulate team metadata inheritance: priority is in user_api_key_auth_metadata
    # This is how the proxy passes team metadata to the callback
    kwargs = {
        "standard_logging_object": {
            "metadata": {
                # Priority NOT at top level (this would fail before the fix)
                # Priority IS in user_api_key_auth_metadata (team inheritance)
                "user_api_key_auth_metadata": {"priority": "team_priority"},
            },
            "model_group": model,
        },
        "litellm_params": {
            "metadata": {"model_group": model},
        },
    }

    with patch(
        "litellm.proxy.common_utils.callback_utils.get_model_group_from_litellm_kwargs",
        return_value=model,
    ):
        await handler.async_log_success_event(
            kwargs=kwargs,
            response_obj=mock_response,
            start_time=None,
            end_time=None,
        )

    # Verify the priority_model key uses 'team_priority' (not 'default_pool')
    priority_keys = [k for k in incremented_keys if "priority_model" in k]
    assert len(priority_keys) == 1, f"Expected 1 priority_model key, got {len(priority_keys)}"

    # The key should contain 'team_priority', not 'default_pool'
    assert "team_priority" in priority_keys[0], (
        f"Expected priority key to use 'team_priority' from user_api_key_auth_metadata, got key: {priority_keys[0]}"
    )
    assert "default_pool" not in priority_keys[0], (
        f"Priority key should NOT use 'default_pool', should use team's priority. Got: {priority_keys[0]}"
    )


@pytest.mark.asyncio
async def test_priority_429_includes_model_name_and_configured_limits():
    """
    The HTB 429 should tell operators which model was hit and what
    the model's configured RPM is, so they can decide whether to tune the
    priority allocation or the model limits.

    Regression test for the previous message that read:
        "Priority-based rate limit exceeded. Priority: prod,
         Rate limit type: tokens, Remaining: -664145,
         Model saturation: 86.3%"
    -- with no indication of which model was hit.
    """
    os.environ["LITELLM_LICENSE"] = "test-license-key"
    litellm.priority_reservation = {"prod": 0.5}

    dual_cache = DualCache()
    handler = DynamicRateLimitHandler(internal_usage_cache=dual_cache)

    model = "gpt-4o-test"
    total_rpm = 10_000

    llm_router = Router(
        model_list=[
            {
                "model_name": model,
                "litellm_params": {
                    "model": "gpt-3.5-turbo",
                    "api_key": "test-key",
                    "api_base": "test-base",
                    "rpm": total_rpm,
                },
            }
        ]
    )
    handler.update_variables(llm_router=llm_router)

    model_group_info = handler.llm_router.get_model_group_info(model_group=model)

    over_limit_response = {
        "overall_code": "OVER_LIMIT",
        "statuses": [
            {
                "code": "OVER_LIMIT",
                "descriptor_key": "priority_model",
                "rate_limit_type": "requests",
                "limit_remaining": -100,
                "current_limit": int(total_rpm * 0.5),
            }
        ],
    }

    with patch.object(
        handler.v3_limiter,
        "htb_check_and_increment",
        new=AsyncMock(return_value=over_limit_response),
    ):
        htb_priority.set("prod")
        with pytest.raises(litellm.RateLimitError) as exc_info:
            await handler.async_pre_call_check(
                deployment={"model_name": model},
                parent_otel_span=None,
            )

    err = exc_info.value
    error_msg = err.message

    assert f"Model: {model}" in error_msg, error_msg
    assert f"Model RPM: {total_rpm}" in error_msg, error_msg
    assert "Priority: prod" in error_msg, error_msg
    assert "Rate limit type: requests" in error_msg, error_msg
    assert "Remaining: -100" in error_msg, error_msg
