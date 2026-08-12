#!/usr/bin/env python3
"""
HTB Priority-Based Rate Limiting Test Suite
============================================

Tests priority enforcement, borrowing, and fallback behavior using 4 API keys.
All secrets (API keys, Redis password) are read from environment variables.

Required environment variables:
  HTB_TEST_PRIOR1_KEY      - API key with prior1 metadata
  HTB_TEST_PRIOR2_KEY      - API key with prior2 metadata
  HTB_TEST_PRIOR3_JYAO_KEY - API key with prior3 metadata (user: jyao)
  HTB_TEST_PRIOR3_DQVU_KEY - API key with prior3 metadata (user: dqvu)
  HTB_TEST_REDIS_PASS      - Redis password

Optional environment variables:
  HTB_TEST_PROXY_URL       - Proxy URL (default: https://litellm.adeoaiengine.ecouncil.ae)
  HTB_TEST_KUBECONFIG      - Kubeconfig path (default: ~/.kube/alain-oicm.conf)
  HTB_TEST_REDIS_NS        - Redis namespace (default: redis)
  HTB_TEST_REDIS_POD       - Redis pod name (default: litellm-redis-0)

Model RPM limits:
  - Qwen/Qwen3.5-0.8B:   100 RPM  (no fallbacks)
  - zai-org/GLM-5.2-FP8:  100 RPM  (fallbacks: GLM-5.1 -> MiniMax)
  - zai-org/GLM-5.1-FP8:  100 RPM  (no fallbacks)
  - MiniMaxAI/MiniMax-M3-MXFP8: 500 RPM (no fallbacks)

Priority reservations:
  prior1=0.50 (50 RPM on 100 RPM model)
  prior2=0.30 (30 RPM on 100 RPM model)
  prior3=0.20 (20 RPM on 100 RPM model)
  saturation_threshold=1.0 (borrowing capped at model RPM)

Usage:
  python3 htb_test.py --test 1     # Run test 1 only
  python3 htb_test.py --test 2     # Run test 2 only
  python3 htb_test.py --test 3     # Run test 3 only
  python3 htb_test.py --all        # Run all tests sequentially
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

# ============================================================================
# Configuration (from environment)
# ============================================================================

_REQUIRED_ENV = (
    "HTB_TEST_PRIOR1_KEY",
    "HTB_TEST_PRIOR2_KEY",
    "HTB_TEST_PRIOR3_JYAO_KEY",
    "HTB_TEST_PRIOR3_DQVU_KEY",
    "HTB_TEST_REDIS_PASS",
)

_missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
if _missing:
    print(f"Error: missing required environment variables: {', '.join(_missing)}")
    sys.exit(1)

PROXY_URL = os.environ.get("HTB_TEST_PROXY_URL", "https://litellm.adeoaiengine.ecouncil.ae")
KUBECONFIG = os.environ.get("HTB_TEST_KUBECONFIG", os.path.expanduser("~/.kube/alain-oicm.conf"))
REDIS_NS = os.environ.get("HTB_TEST_REDIS_NS", "redis")
REDIS_POD = os.environ.get("HTB_TEST_REDIS_POD", "litellm-redis-0")
REDIS_PASS = os.environ["HTB_TEST_REDIS_PASS"]

KEYS = {
    "prior1": {
        "key": os.environ["HTB_TEST_PRIOR1_KEY"],
        "user": "maalmarri@ECOUNCIL.AE",
        "weight": 0.50,
        "guaranteed_100rpm": 50,
    },
    "prior2": {
        "key": os.environ["HTB_TEST_PRIOR2_KEY"],
        "user": "naAlkhazraji@ECOUNCIL.AE",
        "weight": 0.30,
        "guaranteed_100rpm": 30,
    },
    "prior3-jyao": {
        "key": os.environ["HTB_TEST_PRIOR3_JYAO_KEY"],
        "user": "jyao@ECOUNCIL.AE",
        "weight": 0.20,
        "guaranteed_100rpm": 20,
    },
    "prior3-dqvu": {
        "key": os.environ["HTB_TEST_PRIOR3_DQVU_KEY"],
        "user": "dqvu@ECOUNCIL.AE",
        "weight": 0.20,
        "guaranteed_100rpm": 20,
    },
}

# Models
QWEN_MODEL = "Qwen/Qwen3.5-0.8B"
GLM52_MODEL = "zai-org/GLM-5.2-FP8"
GLM51_MODEL = "zai-org/GLM-5.1-FP8"
MINIMAX_MODEL = "MiniMaxAI/MiniMax-M3-MXFP8"

# Model RPM limits
MODEL_RPM = {
    QWEN_MODEL: 100,
    GLM52_MODEL: 100,
    GLM51_MODEL: 100,
    MINIMAX_MODEL: 500,
}

# Request body with thinking disabled
def make_payload(model: str) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Say OK"}],
        "max_tokens": 1,
        "extra_body": {
            "chat_template_kwargs": {"enable_thinking": False},
        },
    }


# ============================================================================
# Redis utilities
# ============================================================================

def flush_redis():
    """Flush all Redis keys to get a clean state."""
    cmd = (
        f"KUBECONFIG={KUBECONFIG} kubectl exec -n {REDIS_NS} {REDIS_POD} "
        f"-- redis-cli -a {REDIS_PASS} --no-auth-warning FLUSHDB"
    )
    subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print("  [redis] Flushed all keys")


def get_htb_counters(model: str) -> Dict[str, int]:
    """Get HTB counter values from Redis for a given model."""
    htb_hash = f"htb:{model}"
    # Get all keys matching this model's HTB hash
    cmd = (
        f"KUBECONFIG={KUBECONFIG} kubectl exec -n {REDIS_NS} {REDIS_POD} "
        f"-- redis-cli -a {REDIS_PASS} --no-auth-warning KEYS '*{htb_hash}.*'"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    keys = [k.strip() for k in result.stdout.strip().split("\n") if k.strip()]

    counters = {}
    for key in keys:
        # Parse priority from key: {htb:model}:model:priority:field
        parts = key.split(":")
        if len(parts) >= 4:
            field_name = parts[-1]  # e.g., "requests", "ewma", "window", "ts"
            priority = parts[-2] if parts[-2] not in ("window", "requests") else "model_wide"
            if field_name == "requests":
                cmd = (
                    f"KUBECONFIG={KUBECONFIG} kubectl exec -n {REDIS_NS} {REDIS_POD} "
                    f"-- redis-cli -a {REDIS_PASS} --no-auth-warning GET {key}"
                )
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                val = result.stdout.strip()
                if val:
                    counters[priority] = int(val)

    # Also get model-wide counter
    model_wide_key = f"{{{htb_hash}}}:requests"
    cmd = (
        f"KUBECONFIG={KUBECONFIG} kubectl exec -n {REDIS_NS} {REDIS_POD} "
        f"-- redis-cli -a {REDIS_PASS} --no-auth-warning GET {model_wide_key}"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    val = result.stdout.strip()
    if val:
        counters["model_wide"] = int(val)

    return counters


def get_htb_ewma(model: str, priority: str) -> Optional[float]:
    """Get EWMA value for a model+priority from Redis."""
    htb_hash = f"htb:{model}"
    ewma_key = f"{{{htb_hash}}}:{model}:{priority}:ewma"
    cmd = (
        f"KUBECONFIG={KUBECONFIG} kubectl exec -n {REDIS_NS} {REDIS_POD} "
        f"-- redis-cli -a {REDIS_PASS} --no-auth-warning GET {ewma_key}"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    val = result.stdout.strip()
    if val:
        return float(val)
    return None


# ============================================================================
# Request execution
# ============================================================================

@dataclass
class RequestResult:
    priority: str
    model: str
    status: int
    response_model: Optional[str] = None  # Which model actually served the request
    error: Optional[str] = None


async def send_request(
    session: aiohttp.ClientSession,
    api_key: str,
    model: str,
    priority_label: str,
    semaphore: asyncio.Semaphore,
) -> RequestResult:
    """Send a single chat completion request."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = make_payload(model)

    async with semaphore:
        try:
            async with session.post(
                f"{PROXY_URL}/v1/chat/completions",
                headers=headers,
                json=payload,
                ssl=False,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                status = resp.status
                response_model = None
                error = None
                if status == 200:
                    try:
                        body = await resp.json()
                        response_model = body.get("model", model)
                    except Exception:
                        pass
                else:
                    try:
                        body = await resp.json()
                        error = body.get("error", {}).get("message", str(body))[:200]
                    except Exception:
                        error = f"HTTP {status}"
                return RequestResult(
                    priority=priority_label,
                    model=model,
                    status=status,
                    response_model=response_model,
                    error=error,
                )
        except Exception as e:
            return RequestResult(
                priority=priority_label,
                model=model,
                status=0,
                error=str(e)[:200],
            )


async def run_burst(
    requests_config: List[Tuple[str, str, int]],
    concurrency: int = 300,
) -> List[RequestResult]:
    """
    Send a burst of concurrent requests.

    Args:
        requests_config: List of (priority_label, model, count) tuples
        concurrency: Max concurrent requests

    Returns:
        List of RequestResult
    """
    semaphore = asyncio.Semaphore(concurrency)
    connector = aiohttp.TCPConnector(limit=0, ssl=False)

    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = []
        for priority_label, model, count in requests_config:
            key_info = KEYS[priority_label]
            for _ in range(count):
                tasks.append(
                    send_request(session, key_info["key"], model, priority_label, semaphore)
                )
        results = await asyncio.gather(*tasks)

    return list(results)


# ============================================================================
# Analysis & reporting
# ============================================================================

def analyze_results(
    results: List[RequestResult],
    title: str,
    models_in_test: List[str],
) -> Dict[str, Any]:
    """Analyze and print test results."""
    print(f"\n{'='*80}")
    print(f"  {title}")
    print(f"{'='*80}")

    total = len(results)
    ok = [r for r in results if r.status == 200]
    denied = [r for r in results if r.status == 429]
    errors = [r for r in results if r.status not in (200, 429)]

    print(f"\n  Total requests:   {total}")
    print(f"  Successful (200): {len(ok)}")
    print(f"  Rate limited (429): {len(denied)}")
    print(f"  Other errors:     {len(errors)}")

    # Per-priority breakdown
    print(f"\n  --- Per-Priority Breakdown ---")
    priority_stats = {}
    for prio in sorted(set(r.priority for r in results)):
        prio_results = [r for r in results if r.priority == prio]
        prio_ok = [r for r in prio_results if r.status == 200]
        prio_denied = [r for r in prio_results if r.status == 429]
        prio_errors = [r for r in prio_results if r.status not in (200, 429)]
        priority_stats[prio] = {
            "total": len(prio_results),
            "ok": len(prio_ok),
            "denied": len(prio_denied),
            "errors": len(prio_errors),
        }
        print(f"  {prio:15s}: {len(prio_ok):3d} OK, {len(prio_denied):3d} denied, {len(prio_errors):3d} errors (sent {len(prio_results)})")

    # Per-model breakdown (which model actually served the request)
    print(f"\n  --- Per-Model Served (response model) ---")
    model_served = Counter()
    for r in ok:
        model_served[r.response_model or r.model] += 1
    for m, count in sorted(model_served.items()):
        print(f"  {m:45s}: {count:3d} served")

    # Redis counter state
    print(f"\n  --- Redis HTB Counters ---")
    for model in models_in_test:
        counters = get_htb_counters(model)
        if counters:
            print(f"  {model}:")
            for prio, count in sorted(counters.items()):
                print(f"    {prio:15s}: {count}")
        else:
            print(f"  {model}: (no counters)")

    # EWMA values
    print(f"\n  --- EWMA Values ---")
    priorities = ["prior1", "prior2", "prior3"]
    for model in models_in_test:
        for prio in priorities:
            ewma = get_htb_ewma(model, prio)
            if ewma is not None:
                print(f"  {model:45s} {prio:8s}: EWMA={ewma:.2f}")

    print(f"\n{'='*80}\n")

    return {
        "total": total,
        "ok": len(ok),
        "denied": len(denied),
        "errors": len(errors),
        "priority_stats": priority_stats,
        "model_served": dict(model_served),
    }


# ============================================================================
# Test scenarios
# ============================================================================

async def test_1_qwen_all_keys():
    """
    Test 1: All 4 keys testing Qwen/Qwen3.5-0.8B (100 RPM, no fallbacks).

    Expected behavior:
    - prior1 gets 50 RPM guaranteed (50%)
    - prior2 gets 30 RPM guaranteed (30%)
    - prior3 (2 keys sharing 20%) get 20 RPM total guaranteed
    - Borrowing allowed up to saturation cap (80 RPM)
    - Model-wide limit: 100 RPM hard cap
    - No fallbacks → excess requests get 429

    Sub-tests:
      1a: Light load (10 per key) → all should succeed
      1b: Heavy load (80 per key, 320 total) → should see priority distribution
    """
    print("\n" + "█" * 80)
    print("  TEST 1: Qwen/Qwen3.5-0.8B — All 4 Keys")
    print("  Model: 100 RPM, no fallbacks")
    print("  Keys: 1×prior1, 1×prior2, 2×prior3")
    print("█" * 80)

    # --- Sub-test 1a: Light load ---
    print("\n  ▶ Sub-test 1a: Light load (10 per key, 40 total)")
    print("    Expected: All 40 succeed (well within 100 RPM)")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior1", QWEN_MODEL, 10),
        ("prior2", QWEN_MODEL, 10),
        ("prior3-jyao", QWEN_MODEL, 10),
        ("prior3-dqvu", QWEN_MODEL, 10),
    ])
    analyze_results(results, "Test 1a: Qwen Light Load (40 total, 10 per key)", [QWEN_MODEL])

    # --- Sub-test 1b: Heavy load ---
    print("\n  ▶ Sub-test 1b: Heavy load (80 per key, 320 total)")
    print("    Expected: ~100 succeed, ~220 denied")
    print("    prior1 should get ≥50, prior2 ≥30, prior3 (combined) ≥20")
    print("    Borrowing: if prior3 is idle, prior1 can borrow up to 80 RPM")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior1", QWEN_MODEL, 80),
        ("prior2", QWEN_MODEL, 80),
        ("prior3-jyao", QWEN_MODEL, 80),
        ("prior3-dqvu", QWEN_MODEL, 80),
    ])
    analyze_results(results, "Test 1b: Qwen Heavy Load (320 total, 80 per key)", [QWEN_MODEL])

    # --- Sub-test 1c: prior1 heavy, others light ---
    print("\n  ▶ Sub-test 1c: prior1 heavy (80), others light (2 each)")
    print("    Expected: prior1 borrows idle capacity → ~80 OK")
    print("    prior2/prior3 get their 2 requests each")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior1", QWEN_MODEL, 80),
        ("prior2", QWEN_MODEL, 2),
        ("prior3-jyao", QWEN_MODEL, 2),
        ("prior3-dqvu", QWEN_MODEL, 2),
    ])
    analyze_results(results, "Test 1c: Qwen prior1 Heavy, Others Light (86 total)", [QWEN_MODEL])

    # --- Sub-test 1d: prior3 heavy, prior1 light ---
    print("\n  ▶ Sub-test 1d: prior3 heavy (80×2=160), prior1 light (2), prior2 light (2)")
    print("    Expected: prior3 borrows idle capacity → ~80 OK combined")
    print("    prior1 and prior2 get their 2 requests each")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior1", QWEN_MODEL, 2),
        ("prior2", QWEN_MODEL, 2),
        ("prior3-jyao", QWEN_MODEL, 80),
        ("prior3-dqvu", QWEN_MODEL, 80),
    ])
    analyze_results(results, "Test 1d: Qwen prior3 Heavy, Others Light (164 total)", [QWEN_MODEL])


async def test_2_glm52_all_keys():
    """
    Test 2: All 4 keys testing zai-org/GLM-5.2-FP8 (100 RPM, with fallbacks).

    Fallback chain: GLM-5.2 → GLM-5.1 → MiniMax

    Expected behavior:
    - HTB enforced on GLM-5.2 (100 RPM)
    - When GLM-5.2 at capacity, RateLimitError triggers fallback to GLM-5.1
    - HTB enforced on GLM-5.1 (100 RPM) — same priority limits
    - When GLM-5.1 at capacity, fallback to MiniMax (500 RPM)
    - HTB enforced on MiniMax (500 RPM) — priority limits scaled to 500

    Sub-tests:
      2a: Light load → all succeed on GLM-5.2
      2b: Heavy load → overflow cascades through fallback chain
      2c: prior3-only heavy load → tests borrowing + fallback
    """
    print("\n" + "█" * 80)
    print("  TEST 2: zai-org/GLM-5.2-FP8 — All 4 Keys (with fallbacks)")
    print("  Model: 100 RPM, fallbacks: GLM-5.1(100) → MiniMax(500)")
    print("  Keys: 1×prior1, 1×prior2, 2×prior3")
    print("█" * 80)

    # --- Sub-test 2a: Light load ---
    print("\n  ▶ Sub-test 2a: Light load (10 per key, 40 total)")
    print("    Expected: All 40 succeed on GLM-5.2 (within 100 RPM)")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior1", GLM52_MODEL, 10),
        ("prior2", GLM52_MODEL, 10),
        ("prior3-jyao", GLM52_MODEL, 10),
        ("prior3-dqvu", GLM52_MODEL, 10),
    ])
    analyze_results(results, "Test 2a: GLM-5.2 Light Load (40 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])

    # --- Sub-test 2b: Heavy load, all priorities ---
    print("\n  ▶ Sub-test 2b: Heavy load (80 per key, 320 total)")
    print("    Expected: GLM-5.2 fills (100), overflow → GLM-5.1 (100), overflow → MiniMax")
    print("    Total capacity: 100+100+500 = 700 RPM, so all 320 should succeed")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior1", GLM52_MODEL, 80),
        ("prior2", GLM52_MODEL, 80),
        ("prior3-jyao", GLM52_MODEL, 80),
        ("prior3-dqvu", GLM52_MODEL, 80),
    ])
    analyze_results(results, "Test 2b: GLM-5.2 Heavy Load (320 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])

    # --- Sub-test 2c: prior3-only heavy load ---
    print("\n  ▶ Sub-test 2c: prior3 only (80×2=160), prior1/prior2 idle")
    print("    Expected: prior3 borrows idle capacity on each model")
    print("    GLM-5.2: ~80 (borrow to saturation cap)")
    print("    GLM-5.1: ~80 (same)")
    print("    MiniMax: remaining (~0, total 160 < 160 capacity)")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior3-jyao", GLM52_MODEL, 80),
        ("prior3-dqvu", GLM52_MODEL, 80),
    ])
    analyze_results(results, "Test 2c: GLM-5.2 prior3-Only Heavy (160 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])

    # --- Sub-test 2d: prior1 heavy, prior3 heavy, prior2 light ---
    print("\n  ▶ Sub-test 2d: Mixed — prior1 heavy (80), prior3 heavy (80×2=160), prior2 light (2)")
    print("    Expected: prior1 gets priority on each model, prior3 borrows remaining")
    print("    Fallback chain distributes load across 3 models")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior1", GLM52_MODEL, 80),
        ("prior2", GLM52_MODEL, 2),
        ("prior3-jyao", GLM52_MODEL, 80),
        ("prior3-dqvu", GLM52_MODEL, 80),
    ])
    analyze_results(results, "Test 2d: GLM-5.2 Mixed Load (242 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])


async def test_3_glm52_plus_direct_fallback():
    """
    Test 3: Some keys test GLM-5.2, others send directly to fallback models.

    This tests whether HTB enforcement on fallback models works correctly
    when traffic is sent DIRECTLY to those models (not via fallback cascade).

    Setup:
    - prior1 + prior2 send to GLM-5.2 (triggers fallback when at capacity)
    - prior3-jyao sends DIRECTLY to GLM-5.1 (bypasses GLM-5.2)
    - prior3-dqvu sends DIRECTLY to MiniMax (bypasses GLM-5.2 + GLM-5.1)

    Expected behavior:
    - GLM-5.2: HTB enforces 100 RPM for prior1 + prior2
    - GLM-5.1: HTB enforces 100 RPM for prior3-jyao (direct) + overflow from GLM-5.2
    - MiniMax: HTB enforces 500 RPM for prior3-dqvu (direct) + overflow from GLM-5.1

    Sub-tests:
      3a: Light direct + light GLM-5.2
      3b: Heavy direct + heavy GLM-5.2 (stress all 3 models simultaneously)
    """
    print("\n" + "█" * 80)
    print("  TEST 3: GLM-5.2 + Direct Fallback Models")
    print("  prior1, prior2 → GLM-5.2 (fallback chain)")
    print("  prior3-jyao   → GLM-5.1 (direct)")
    print("  prior3-dqvu   → MiniMax (direct)")
    print("█" * 80)

    # --- Sub-test 3a: Light load ---
    print("\n  ▶ Sub-test 3a: Light load (10 per key, 40 total)")
    print("    prior1 (10) → GLM-5.2")
    print("    prior2 (10) → GLM-5.2")
    print("    prior3-jyao (10) → GLM-5.1 (direct)")
    print("    prior3-dqvu (10) → MiniMax (direct)")
    print("    Expected: All 40 succeed")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior1", GLM52_MODEL, 10),
        ("prior2", GLM52_MODEL, 10),
        ("prior3-jyao", GLM51_MODEL, 10),
        ("prior3-dqvu", MINIMAX_MODEL, 10),
    ])
    analyze_results(results, "Test 3a: Direct Fallback Light Load (40 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])

    # --- Sub-test 3b: Heavy load, stress all 3 models ---
    print("\n  ▶ Sub-test 3b: Heavy load (80 per key, 320 total)")
    print("    prior1 (80) → GLM-5.2 (100 RPM, fallback to GLM-5.1 → MiniMax)")
    print("    prior2 (80) → GLM-5.2 (100 RPM, fallback to GLM-5.1 → MiniMax)")
    print("    prior3-jyao (80) → GLM-5.1 direct (100 RPM)")
    print("    prior3-dqvu (80) → MiniMax direct (500 RPM)")
    print("")
    print("    Expected: GLM-5.2 fills at 100, overflow cascades to GLM-5.1")
    print("    GLM-5.1 serves direct prior3-jyao + overflow from GLM-5.2, fills at 100")
    print("    MiniMax serves direct prior3-dqvu + overflow from GLM-5.1")
    print("    Total: 320 requests, total capacity 700 → all should succeed")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior1", GLM52_MODEL, 80),
        ("prior2", GLM52_MODEL, 80),
        ("prior3-jyao", GLM51_MODEL, 80),
        ("prior3-dqvu", MINIMAX_MODEL, 80),
    ])
    analyze_results(results, "Test 3b: Direct Fallback Heavy Load (320 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])

    # --- Sub-test 3c: prior3 direct to fallback models, prior1 on GLM-5.2 ---
    print("\n  ▶ Sub-test 3c: prior1 heavy on GLM-5.2, prior3 direct on fallbacks")
    print("    prior1 (80) → GLM-5.2")
    print("    prior3-jyao (80) → GLM-5.1 (direct)")
    print("    prior3-dqvu (80) → MiniMax (direct)")
    print("    prior2 (2) → GLM-5.2 (light)")
    print("")
    print("    Expected: prior1 gets 50 on GLM-5.2, borrows to ~80")
    print("    prior3-jyao gets 20 guaranteed on GLM-5.1, borrows to ~80")
    print("    prior3-dqvu gets 100 guaranteed on MiniMax (20% of 500)")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior1", GLM52_MODEL, 80),
        ("prior2", GLM52_MODEL, 2),
        ("prior3-jyao", GLM51_MODEL, 80),
        ("prior3-dqvu", MINIMAX_MODEL, 80),
    ])
    analyze_results(results, "Test 3c: prior1 on GLM-5.2, prior3 Direct on Fallbacks (242 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])

    # --- Sub-test 3d: All prior3, split between GLM-5.2 and direct fallbacks ---
    print("\n  ▶ Sub-test 3d: All prior3, split between GLM-5.2 and direct fallbacks")
    print("    prior3-jyao (80) → GLM-5.2 (fallback chain)")
    print("    prior3-dqvu (80) → MiniMax (direct, bypasses chain)")
    print("")
    print("    Expected: GLM-5.2: prior3 borrows to ~80, overflow → GLM-5.1 → MiniMax")
    print("    MiniMax: prior3-dqvu direct (80) + overflow from GLM-5.1")
    print("    Both prior3 keys share the same priority pool on each model")

    flush_redis()
    await asyncio.sleep(2)

    results = await run_burst([
        ("prior3-jyao", GLM52_MODEL, 80),
        ("prior3-dqvu", MINIMAX_MODEL, 80),
    ])
    analyze_results(results, "Test 3d: prior3 Split GLM-5.2 + Direct MiniMax (160 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])


# ============================================================================
# Main
# ============================================================================

async def main():
    parser = argparse.ArgumentParser(description="HTB Priority Rate Limiting Test Suite")
    parser.add_argument("--test", type=int, choices=[1, 2, 3], help="Run specific test")
    parser.add_argument("--all", action="store_true", help="Run all tests sequentially")
    args = parser.parse_args()

    if not args.test and not args.all:
        parser.print_help()
        sys.exit(1)

    print("\n" + "=" * 80)
    print("  HTB Priority-Based Rate Limiting Test Suite")
    print("  Priority Reservation: prior1=0.50, prior2=0.30, prior3=0.20")
    print("  Saturation Threshold: 0.80")
    print("  Thinking: DISABLED (chat_template_kwargs.enable_thinking=False)")
    print("=" * 80)

    if args.test == 1 or args.all:
        await test_1_qwen_all_keys()
        if args.all:
            print("\n  ⏳ Waiting 65s for window reset before next test...")
            await asyncio.sleep(65)

    if args.test == 2 or args.all:
        await test_2_glm52_all_keys()
        if args.all:
            print("\n  ⏳ Waiting 65s for window reset before next test...")
            await asyncio.sleep(65)

    if args.test == 3 or args.all:
        await test_3_glm52_plus_direct_fallback()

    print("\n" + "=" * 80)
    print("  All tests complete!")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
