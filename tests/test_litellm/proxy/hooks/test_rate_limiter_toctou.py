"""
Tests validating TOCTOU race condition in batch + dynamic rate limiters.

Issue: rate-limit check (read_only=True) and counter increment happen as two
separate awaits. Concurrent requests all observe the same pre-increment state,
all pass validation, then all increment — bypassing the limit.

Vulnerable code paths:
- litellm/proxy/hooks/batch_rate_limiter.py:181-248
  (_check_and_increment_batch_counters: should_rate_limit(read_only=True)
  → validate → async_increment_tokens_with_ttl_preservation)
- litellm/proxy/hooks/dynamic_rate_limiter_v3.py:463-548
  (_check_rate_limits PHASE 1 read_only check → PHASE 3 increment)

These tests EXPECTED to fail against current (vulnerable) code and pass once
check-and-increment becomes atomic.
"""

import asyncio
import os
import sys
from typing import List

import pytest

sys.path.insert(0, os.path.abspath("../../../.."))

import litellm
from litellm import DualCache, Router
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.hooks.batch_rate_limiter import BatchFileUsage
from litellm.proxy.hooks.dynamic_rate_limiter_v3_htb import (
    _PROXY_DynamicRateLimitHandlerV3Htb as DynamicRateLimitHandler,
)
from litellm.proxy.hooks.parallel_request_limiter_v3 import (
    _PROXY_MaxParallelRequestsHandler_v3,
)
from litellm.proxy.utils import InternalUsageCache, hash_token


def _make_phase1_barrier(num_concurrent: int, timeout: float = 0.1):
    """
    Sync primitive that, pre-fix, forces all N concurrent coroutines to finish
    their read-only Phase 1 check before any proceeds to Phase 3 increment —
    mimicking asyncio I/O scheduling under load on the vulnerable code.

    Wraps `should_rate_limit` so on `read_only=True` calls it waits until N
    callers arrive (TOCTOU window opened) OR `timeout` elapses (post-fix path:
    the limiter's serialization lock prevents N from ever reaching the
    barrier; the timeout lets the holder proceed so the lock can do its job).

    Pre-fix: barrier fills before timeout → all see same state → bypass observed.
    Post-fix: only lock-holder reaches barrier → times out → serial execution
    enforces limit.
    """
    arrived = 0
    all_arrived = asyncio.Event()

    def wrap(original):
        async def patched(*args, **kwargs):
            result = await original(*args, **kwargs)
            if kwargs.get("read_only"):
                nonlocal arrived
                arrived += 1
                if arrived >= num_concurrent:
                    all_arrived.set()
                try:
                    await asyncio.wait_for(all_arrived.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
            return result

        return patched

    return wrap


@pytest.mark.asyncio
async def test_batch_limiter_concurrent_bypasses_tpm_via_toctou():
    """
    5 concurrent batch submissions of 40 tokens each against TPM=100 limit.

    Sequential semantics: only 2 batches fit (2 * 40 = 80 ≤ 100, 3rd at 120 fails).
    With TOCTOU: all 5 succeed → 200 tokens consumed, 100% over limit.

    Demonstrates batch_rate_limiter.py:183-248 multi-phase flaw.
    """
    NUM_CONCURRENT = 5
    BATCH_TOKENS = 40
    TPM_LIMIT = 100

    dual_cache = DualCache()
    internal_usage_cache = InternalUsageCache(dual_cache=dual_cache)
    rate_limiter = _PROXY_MaxParallelRequestsHandler_v3(internal_usage_cache=internal_usage_cache)
    batch_limiter = rate_limiter._get_batch_rate_limiter()
    assert batch_limiter is not None

    user_api_key_dict = UserAPIKeyAuth(
        api_key=hash_token("toctou-batch-key"),
        tpm_limit=TPM_LIMIT,
        rpm_limit=1000,
    )
    batch_usage = BatchFileUsage(total_tokens=BATCH_TOKENS, request_count=1)

    barrier = _make_phase1_barrier(NUM_CONCURRENT)
    rate_limiter.should_rate_limit = barrier(rate_limiter.should_rate_limit)

    results = await asyncio.gather(
        *[
            batch_limiter._check_and_increment_batch_counters(
                user_api_key_dict=user_api_key_dict,
                data={},
                batch_usage=batch_usage,
            )
            for _ in range(NUM_CONCURRENT)
        ],
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, Exception)]
    rejections = [r for r in results if isinstance(r, Exception)]
    total_consumed = len(successes) * BATCH_TOKENS
    max_allowed_successes = TPM_LIMIT // BATCH_TOKENS  # 2

    assert len(successes) <= max_allowed_successes, (
        f"TOCTOU bypass: {len(successes)}/{NUM_CONCURRENT} concurrent batches "
        f"passed despite TPM={TPM_LIMIT}. Consumed {total_consumed} tokens "
        f"({total_consumed - TPM_LIMIT} over limit). "
        f"Atomic check-and-increment would allow ≤{max_allowed_successes}. "
        f"Rejections: {len(rejections)}"
    )


@pytest.mark.asyncio
async def test_batch_limiter_uses_atomic_check_and_increment():
    """
    Regression test: batch limiter routes through
    `atomic_check_and_increment_by_n` rather than the legacy two-phase
    pattern (read_only=True check + separate async_increment_tokens_with_ttl_preservation).

    Ensures future refactors don't reintroduce the TOCTOU window.
    """
    dual_cache = DualCache()
    internal_usage_cache = InternalUsageCache(dual_cache=dual_cache)
    rate_limiter = _PROXY_MaxParallelRequestsHandler_v3(internal_usage_cache=internal_usage_cache)
    batch_limiter = rate_limiter._get_batch_rate_limiter()
    assert batch_limiter is not None

    call_log: List[str] = []
    original_atomic = rate_limiter.atomic_check_and_increment_by_n
    original_should = rate_limiter.should_rate_limit

    async def logging_atomic(*args, **kwargs):
        call_log.append("atomic_check_and_increment_by_n")
        return await original_atomic(*args, **kwargs)

    async def logging_should(*args, **kwargs):
        call_log.append(f"should_rate_limit(read_only={kwargs.get('read_only')})")
        return await original_should(*args, **kwargs)

    rate_limiter.atomic_check_and_increment_by_n = logging_atomic
    rate_limiter.should_rate_limit = logging_should

    user_api_key_dict = UserAPIKeyAuth(
        api_key=hash_token("atomic-test-key"),
        tpm_limit=10000,
        rpm_limit=1000,
    )

    await batch_limiter._check_and_increment_batch_counters(
        user_api_key_dict=user_api_key_dict,
        data={},
        batch_usage=BatchFileUsage(total_tokens=50, request_count=1),
    )

    assert "atomic_check_and_increment_by_n" in call_log, (
        f"Batch limiter must route through atomic_check_and_increment_by_n. Calls observed: {call_log}"
    )
    legacy_calls = [c for c in call_log if c.startswith("should_rate_limit(")]
    assert not legacy_calls, (
        f"Batch limiter must not call should_rate_limit directly (legacy two-phase pattern). Observed: {legacy_calls}"
    )


@pytest.mark.asyncio
async def test_dynamic_rate_limiter_v3_concurrent_bypasses_model_capacity():
    """
    The HTB dynamic limiter uses an atomic Lua script
    (htb_check_and_increment) that checks and increments in one step,
    eliminating the TOCTOU race that the legacy two-phase approach had.

    With RPM=2 model capacity and 10 concurrent requests, the atomic
    Lua script must not let more than RPM+1 through (strict > comparison
    allows counter==RPM to pass before the next sees counter > RPM).
    """
    from litellm.proxy.hooks.dynamic_rate_limiter_v3_htb import htb_priority

    NUM_CONCURRENT = 10
    MODEL_RPM = 2
    MAX_SEQUENTIAL_SUCCESSES = MODEL_RPM + 1

    os.environ["LITELLM_LICENSE"] = "test-license-key"
    litellm.priority_reservation = {"high": 0.9, "low": 0.1}

    dual_cache = DualCache()
    handler = DynamicRateLimitHandler(internal_usage_cache=dual_cache)

    model = "toctou-dyn-model"
    llm_router = Router(
        model_list=[
            {
                "model_name": model,
                "litellm_params": {
                    "model": "gpt-3.5-turbo",
                    "api_key": "test-key",
                    "api_base": "test-base",
                    "rpm": MODEL_RPM,
                },
            }
        ]
    )
    handler.update_variables(llm_router=llm_router)

    async def one_request(idx: int):
        htb_priority.set("high")
        try:
            await handler.async_pre_call_check(
                deployment={"model_name": model},
                parent_otel_span=None,
            )
            return "OK"
        except Exception as e:
            return e

    results = await asyncio.gather(
        *[one_request(i) for i in range(NUM_CONCURRENT)],
        return_exceptions=True,
    )
    successes = [r for r in results if r == "OK"]

    assert len(successes) <= MAX_SEQUENTIAL_SUCCESSES, (
        f"HTB atomic check should not let more than {MAX_SEQUENTIAL_SUCCESSES} "
        f"concurrent requests through with RPM={MODEL_RPM}. "
        f"Got {len(successes)}/{NUM_CONCURRENT} successes."
    )


@pytest.mark.asyncio
async def test_dynamic_rate_limiter_v3_uses_atomic_check_and_increment():
    """
    Regression test: the HTB dynamic limiter routes through
    `htb_check_and_increment`, which is an atomic Lua-script-based
    check-and-increment. This replaces the legacy two-phase
    read_only=True check followed by a separate read_only=False increment
    that was vulnerable to TOCTOU races.
    """
    from litellm.proxy.hooks.dynamic_rate_limiter_v3_htb import htb_priority

    os.environ["LITELLM_LICENSE"] = "test-license-key"
    litellm.priority_reservation = {"high": 0.9, "low": 0.1}

    dual_cache = DualCache()
    handler = DynamicRateLimitHandler(internal_usage_cache=dual_cache)

    model = "atomic-dyn-model"
    llm_router = Router(
        model_list=[
            {
                "model_name": model,
                "litellm_params": {
                    "model": "gpt-3.5-turbo",
                    "api_key": "test-key",
                    "api_base": "test-base",
                    "rpm": 100,
                },
            }
        ]
    )
    handler.update_variables(llm_router=llm_router)

    htb_calls: List[dict] = []
    original_htb = handler.v3_limiter.htb_check_and_increment

    async def logging_htb(*args, **kwargs):
        htb_calls.append(kwargs)
        return await original_htb(*args, **kwargs)

    handler.v3_limiter.htb_check_and_increment = logging_htb

    htb_priority.set("high")
    await handler.async_pre_call_check(
        deployment={"model_name": model},
        parent_otel_span=None,
    )

    assert htb_calls, (
        "Dynamic limiter must route through htb_check_and_increment "
        "(atomic Lua script, no legacy two-phase read_only pattern)."
    )
    assert htb_calls[0].get("priority_descriptor") is not None, (
        f"Expected priority_descriptor in htb_check_and_increment call. Got: {htb_calls}"
    )


@pytest.mark.asyncio
async def test_batch_zero_token_consumes_rpm_only():
    """
    Zero-token batch (e.g. metadata-only call) should still increment RPM
    counter but NOT TPM counter.

    Edge case from review: `if inc_amount <= 0: continue` in
    `atomic_check_and_increment_by_n` skips descriptor counters whose
    increment is zero. Verifies asymmetric quota consumption is intentional
    and observable: an RPM-bounded but TPM-free request path stays bounded
    by RPM alone.
    """
    dual_cache = DualCache()
    internal_usage_cache = InternalUsageCache(dual_cache=dual_cache)
    rate_limiter = _PROXY_MaxParallelRequestsHandler_v3(internal_usage_cache=internal_usage_cache)
    batch_limiter = rate_limiter._get_batch_rate_limiter()
    assert batch_limiter is not None

    user_api_key_dict = UserAPIKeyAuth(
        api_key=hash_token("zero-token-key"),
        tpm_limit=100,
        rpm_limit=3,
    )
    zero_batch = BatchFileUsage(total_tokens=0, request_count=1)

    # 3 zero-token batches must succeed (RPM=3 allows). 4th must hit RPM cap,
    # NOT TPM (because token counter never increments past 0).
    for i in range(3):
        await batch_limiter._check_and_increment_batch_counters(
            user_api_key_dict=user_api_key_dict,
            data={},
            batch_usage=zero_batch,
        )

    # Inspect counters: RPM key incremented to 3, TPM key absent (or 0).
    rpm_key = rate_limiter.create_rate_limit_keys("api_key", user_api_key_dict.api_key or "", "requests")
    tpm_key = rate_limiter.create_rate_limit_keys("api_key", user_api_key_dict.api_key or "", "tokens")
    rpm_val = await internal_usage_cache.async_get_cache(key=rpm_key, litellm_parent_otel_span=None, local_only=True)
    tpm_val = await internal_usage_cache.async_get_cache(key=tpm_key, litellm_parent_otel_span=None, local_only=True)
    assert int(rpm_val or 0) == 3, f"RPM counter must reach 3, got {rpm_val}"
    assert tpm_val in (
        None,
        0,
        "0",
    ), f"TPM counter must remain unset/0 for zero-token batches, got {tpm_val}"

    # 4th attempt: RPM exhausted -> 429.
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await batch_limiter._check_and_increment_batch_counters(
            user_api_key_dict=user_api_key_dict,
            data={},
            batch_usage=zero_batch,
        )
    assert exc.value.status_code == 429


@pytest.mark.asyncio
async def test_dynamic_rate_limiter_v3_fails_closed_on_unknown_descriptor():
    """
    Fail-closed guard: when htb_check_and_increment returns
    overall_code=OVER_LIMIT, the dynamic limiter must raise a 429
    rather than silently fall through, regardless of which descriptor
    triggered the limit.

    Reproduces by patching htb_check_and_increment to return an
    OVER_LIMIT response carrying an unknown descriptor_key.
    """
    from unittest.mock import AsyncMock

    from litellm.proxy.hooks.dynamic_rate_limiter_v3_htb import htb_priority

    os.environ["LITELLM_LICENSE"] = "test-license-key"
    litellm.priority_reservation = {"high": 0.9, "low": 0.1}

    dual_cache = DualCache()
    handler = DynamicRateLimitHandler(internal_usage_cache=dual_cache)

    model = "fail-closed-model"
    llm_router = Router(
        model_list=[
            {
                "model_name": model,
                "litellm_params": {
                    "model": "gpt-3.5-turbo",
                    "api_key": "test-key",
                    "api_base": "test-base",
                    "rpm": 100,
                },
            }
        ]
    )
    handler.update_variables(llm_router=llm_router)

    handler.v3_limiter.htb_check_and_increment = AsyncMock(
        return_value={
            "overall_code": "OVER_LIMIT",
            "statuses": [
                {
                    "code": "OVER_LIMIT",
                    "current_limit": 100,
                    "limit_remaining": 0,
                    "rate_limit_type": "requests",
                    "descriptor_key": "future_unrecognized_descriptor",
                }
            ],
        }
    )

    htb_priority.set("high")
    with pytest.raises(litellm.RateLimitError) as exc:
        await handler.async_pre_call_check(
            deployment={"model_name": model},
            parent_otel_span=None,
        )
    assert exc.value.status_code == 429, f"Expected 429 fail-closed on unknown descriptor; got {exc.value.status_code}"
