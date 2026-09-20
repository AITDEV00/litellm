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
  python3 htb_test_v2.py --test 1     # Run test 1 only
  python3 htb_test_v2.py --test 2     # Run test 2 only
  python3 htb_test_v2.py --test 3     # Run test 3 only
  python3 htb_test_v2.py --all        # Run all tests sequentially
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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

def _redis_exec(args: str) -> str:
    """Execute a redis-cli command inside the Redis pod."""
    cmd = (
        f"KUBECONFIG={KUBECONFIG} kubectl exec -n {REDIS_NS} {REDIS_POD} "
        f"-- redis-cli -a {REDIS_PASS} --no-auth-warning {args}"
    )
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return result.stdout.strip()


def flush_redis():
    """Flush all Redis keys to get a clean state."""
    _redis_exec("FLUSHDB")
    print("  [redis] Flushed all keys")


def get_htb_counters(model: str) -> Dict[str, int]:
    """Get HTB counter values from Redis for a given model."""
    htb_hash = f"htb:{model}"
    counters = {}

    # Get all keys matching this model's HTB hash tag
    pattern = f"*{htb_hash}*"
    keys_raw = _redis_exec(f"KEYS '{pattern}'")
    keys = [k.strip() for k in keys_raw.split("\n") if k.strip()]

    for key in keys:
        # Parse the key structure: {htb:model}:model:priority:field
        # or {htb:model}:requests (model-wide)
        # The hash tag {htb:model} ensures same hash slot
        inner = key.replace(f"{{{htb_hash}}}", "HTBHASH")
        parts = inner.split(":")

        if "requests" in parts:
            val = _redis_exec(f"GET {key}")
            if val and val != "(nil)":
                # Determine if model-wide or per-priority
                if len(parts) == 2 and parts[0] == "HTBHASH" and parts[1] == "requests":
                    counters["model_wide"] = int(val)
                elif len(parts) >= 4:
                    priority = parts[2]
                    counters[f"{priority}"] = int(val)

    return counters


def get_htb_ewma(model: str, priority: str) -> Optional[float]:
    """Get EWMA value for a model+priority from Redis."""
    htb_hash = f"htb:{model}"
    ewma_key = f"{{{htb_hash}}}:{model}:{priority}:ewma"
    val = _redis_exec(f"GET {ewma_key}")
    if val and val != "(nil)":
        return float(val)
    return None


def get_all_htb_state(model: str) -> Dict[str, str]:
    """Get all HTB-related keys and values for a model from Redis."""
    htb_hash = f"htb:{model}"
    pattern = f"*{htb_hash}*"
    keys_raw = _redis_exec(f"KEYS '{pattern}'")
    keys = [k.strip() for k in keys_raw.split("\n") if k.strip()]

    state = {}
    for key in keys:
        val = _redis_exec(f"GET {key}")
        if val and val != "(nil)":
            state[key] = val

    return state


# ============================================================================
# Request execution
# ============================================================================

@dataclass
class RequestResult:
    priority: str
    model: str
    status: int
    response_model: Optional[str] = None
    error: Optional[str] = None


def _create_session() -> requests.Session:
    """Create a requests session with connection pooling."""
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=1, pool_maxsize=1, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# Thread-local session storage
_thread_local = threading.local()

def _get_thread_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = _create_session()
    return _thread_local.session


def send_request(
    api_key: str,
    model: str,
    priority_label: str,
) -> RequestResult:
    """Send a single chat completion request."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = make_payload(model)

    try:
        session = _get_thread_session()
        resp = session.post(
            f"{PROXY_URL}/v1/chat/completions",
            headers=headers,
            json=payload,
            verify=False,
            timeout=120,
        )
        status = resp.status_code
        response_model = None
        error = None
        if status == 200:
            try:
                body = resp.json()
                response_model = body.get("model", model)
            except Exception:
                pass
        else:
            try:
                body = resp.json()
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


def run_burst(
    requests_config: List[Tuple[str, str, int]],
    max_workers: int = 300,
) -> List[RequestResult]:
    """
    Send a burst of concurrent requests.

    Args:
        requests_config: List of (priority_label, model, count) tuples
        max_workers: Max concurrent threads

    Returns:
        List of RequestResult
    """
    # Build the list of all individual request specs
    all_specs = []
    for priority_label, model, count in requests_config:
        key_info = KEYS[priority_label]
        for _ in range(count):
            all_specs.append((key_info["key"], model, priority_label))

    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(send_request, api_key, model, priority_label)
            for api_key, model, priority_label in all_specs
        ]
        for future in as_completed(futures):
            results.append(future.result())

    return results


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
        guaranteed = KEYS.get(prio, {}).get("guaranteed_100rpm", "?")
        print(f"  {prio:15s}: {len(prio_ok):3d} OK, {len(prio_denied):3d} denied, {len(prio_errors):3d} errors (sent {len(prio_results)}, guaranteed={guaranteed} RPM)")

    # Per-model breakdown (which model actually served the request)
    print(f"\n  --- Per-Model Served (response model) ---")
    model_served = Counter()
    for r in ok:
        model_served[r.response_model or r.model] += 1
    if model_served:
        for m, count in sorted(model_served.items()):
            print(f"  {m:45s}: {count:3d} served")
    else:
        print("  (none)")

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
        found_any = False
        for prio in priorities:
            ewma = get_htb_ewma(model, prio)
            if ewma is not None:
                print(f"  {model:45s} {prio:8s}: EWMA={ewma:.2f}")
                found_any = True
        if not found_any:
            print(f"  {model:45s}: (no EWMA values)")

    # Full Redis state for debugging
    print(f"\n  --- Full Redis HTB State ---")
    for model in models_in_test:
        state = get_all_htb_state(model)
        if state:
            print(f"  {model}:")
            for key, val in sorted(state.items()):
                print(f"    {key}: {val}")
        else:
            print(f"  {model}: (empty)")

    # Validation
    print(f"\n  --- Validation ---")
    model_rpm = MODEL_RPM
    for model in models_in_test:
        counters = get_htb_counters(model)
        model_wide = counters.get("model_wide", 0)
        rpm_limit = model_rpm.get(model, 0)
        if model_wide > 0:
            pct = (model_wide / rpm_limit * 100) if rpm_limit > 0 else 0
            status = "✓" if model_wide <= rpm_limit else "✗ EXCEEDED"
            print(f"  {model}: model_wide={model_wide}/{rpm_limit} RPM ({pct:.0f}%) {status}")

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

def test_1_qwen_all_keys():
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
      1c: prior1 heavy, others light → tests borrowing
      1d: prior3 heavy, others light → tests borrowing for low priority
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
    time.sleep(2)

    results = run_burst([
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
    time.sleep(2)

    results = run_burst([
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
    time.sleep(2)

    results = run_burst([
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
    time.sleep(2)

    results = run_burst([
        ("prior1", QWEN_MODEL, 2),
        ("prior2", QWEN_MODEL, 2),
        ("prior3-jyao", QWEN_MODEL, 80),
        ("prior3-dqvu", QWEN_MODEL, 80),
    ])
    analyze_results(results, "Test 1d: Qwen prior3 Heavy, Others Light (164 total)", [QWEN_MODEL])


def test_2_glm52_all_keys():
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
      2d: Mixed priorities heavy load
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
    time.sleep(2)

    results = run_burst([
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
    time.sleep(2)

    results = run_burst([
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
    time.sleep(2)

    results = run_burst([
        ("prior3-jyao", GLM52_MODEL, 80),
        ("prior3-dqvu", GLM52_MODEL, 80),
    ])
    analyze_results(results, "Test 2c: GLM-5.2 prior3-Only Heavy (160 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])

    # --- Sub-test 2d: prior1 heavy, prior3 heavy, prior2 light ---
    print("\n  ▶ Sub-test 2d: Mixed — prior1 heavy (80), prior3 heavy (80×2=160), prior2 light (2)")
    print("    Expected: prior1 gets priority on each model, prior3 borrows remaining")
    print("    Fallback chain distributes load across 3 models")

    flush_redis()
    time.sleep(2)

    results = run_burst([
        ("prior1", GLM52_MODEL, 80),
        ("prior2", GLM52_MODEL, 2),
        ("prior3-jyao", GLM52_MODEL, 80),
        ("prior3-dqvu", GLM52_MODEL, 80),
    ])
    analyze_results(results, "Test 2d: GLM-5.2 Mixed Load (242 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])


def test_3_glm52_plus_direct_fallback():
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
      3c: prior1 on GLM-5.2, prior3 direct on fallbacks
      3d: All prior3, split between GLM-5.2 and direct fallbacks
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
    time.sleep(2)

    results = run_burst([
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
    time.sleep(2)

    results = run_burst([
        ("prior1", GLM52_MODEL, 80),
        ("prior2", GLM52_MODEL, 80),
        ("prior3-jyao", GLM51_MODEL, 80),
        ("prior3-dqvu", MINIMAX_MODEL, 80),
    ])
    analyze_results(results, "Test 3b: Direct Fallback Heavy Load (320 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])

    # --- Sub-test 3c: prior1 on GLM-5.2, prior3 direct on fallbacks ---
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
    time.sleep(2)

    results = run_burst([
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
    time.sleep(2)

    results = run_burst([
        ("prior3-jyao", GLM52_MODEL, 80),
        ("prior3-dqvu", MINIMAX_MODEL, 80),
    ])
    analyze_results(results, "Test 3d: prior3 Split GLM-5.2 + Direct MiniMax (160 total)", [GLM52_MODEL, GLM51_MODEL, MINIMAX_MODEL])


# ============================================================================
# Main
# ============================================================================

def main():
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
        test_1_qwen_all_keys()
        if args.all:
            print("\n  ⏳ Waiting 65s for window reset before next test...")
            time.sleep(65)

    if args.test == 2 or args.all:
        test_2_glm52_all_keys()
        if args.all:
            print("\n  ⏳ Waiting 65s for window reset before next test...")
            time.sleep(65)

    if args.test == 3 or args.all:
        test_3_glm52_plus_direct_fallback()

    print("\n" + "=" * 80)
    print("  All tests complete!")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
