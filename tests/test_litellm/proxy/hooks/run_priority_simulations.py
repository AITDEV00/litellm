"""
HTB priority behaviour simulation script.

Runs several traffic scenarios against the real DynamicRateLimitHandlerV3
with the ADEO config (prior1=0.50, prior2=0.30, prior3=0.20, 180 RPM,
saturation_threshold=1.0) and prints a table of results for each scenario.

Uses the in-memory HTB fallback (no Redis required) via async_pre_call_check.

Usage:
    uv run python tests/test_litellm/proxy/hooks/run_priority_simulations.py
"""
# ruff: noqa: T201

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath("../../../.."))

import litellm
from litellm import DualCache, Router
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.hooks.dynamic_rate_limiter_v3 import (
    _PROXY_DynamicRateLimitHandlerV3 as DynamicRateLimitHandler,
    htb_priority,
)
from litellm.types.utils import PriorityReservationSettings

MODEL = "Qwen/Qwen3.5-0.8B"
MODEL_RPM = 180
PRIOR1_RPM = 90
PRIOR2_RPM = 54
PRIOR3_RPM = 36
SATURATION_THRESHOLD = 1.0


def setup_handler():
    os.environ["LITELLM_LICENSE"] = "test-license-key"
    litellm.priority_reservation = {"prior1": 0.50, "prior2": 0.30, "prior3": 0.20}
    litellm.priority_reservation_settings = PriorityReservationSettings(
        saturation_threshold=SATURATION_THRESHOLD,
        default_priority=0.25,
    )
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
    return h, dual_cache


def make_user(priority: str, uid: str) -> UserAPIKeyAuth:
    u = UserAPIKeyAuth()
    u.metadata = {"priority": priority}
    u.user_id = uid
    return u


def make_deployment(model_name: str = MODEL) -> dict:
    return {
        "model_name": model_name,
        "litellm_params": {"model": f"openai/{model_name}"},
    }


async def run_scenario(
    name: str,
    description: str,
    traffic: dict[str, int],
) -> dict:
    """
    Run a single traffic scenario using the in-memory HTB fallback.

    Args:
        name: Short scenario name
        description: Human-readable description
        traffic: {"prior1": N, "prior2": M, "prior3": K} requests per priority

    Returns:
        Dict with results
    """
    handler, dual_cache = setup_handler()
    handler.internal_usage_cache.dual_cache = dual_cache

    success = {"prior1": 0, "prior2": 0, "prior3": 0}
    blocked = {"prior1": 0, "prior2": 0, "prior3": 0}

    users = {
        "prior1": make_user("prior1", "u_p1"),
        "prior2": make_user("prior2", "u_p2"),
        "prior3": make_user("prior3", "u_p3"),
    }

    async def make_request(priority_name: str):
        htb_priority.set(priority_name)
        try:
            await handler.async_pre_call_check(
                deployment=make_deployment(),
                parent_otel_span=None,
            )
            success[priority_name] += 1
        except litellm.RateLimitError:
            blocked[priority_name] += 1

    tasks = []
    for p, count in traffic.items():
        tasks.extend([make_request(p) for _ in range(count)])

    await asyncio.gather(*tasks)

    total_sent = sum(traffic.values())
    total_success = sum(success.values())
    total_blocked = sum(blocked.values())

    return {
        "name": name,
        "description": description,
        "traffic": dict(traffic),
        "success": dict(success),
        "blocked": dict(blocked),
        "total_sent": total_sent,
        "total_success": total_success,
        "total_blocked": total_blocked,
        "model_capacity": MODEL_RPM,
        "prior1_reserved": PRIOR1_RPM,
        "prior2_reserved": PRIOR2_RPM,
        "prior3_reserved": PRIOR3_RPM,
    }


def print_result_table(r: dict):
    name = r["name"]
    desc = r["description"]
    traffic = r["traffic"]
    success = r["success"]
    blocked = r["blocked"]
    total_s = r["total_success"]
    total_b = r["total_blocked"]
    total_sent = r["total_sent"]

    print(f"\n{'=' * 90}")
    print(f"  Scenario: {name}")
    print(f"  {desc}")
    print(f"{'=' * 90}")
    print(f"  {'Priority':<12} {'Sent':>6} {'Allowed':>8} {'Blocked':>8} {'Success%':>10} {'Share':>8} {'Reserved':>9}")
    print(f"  {'-' * 75}")

    for p in ("prior1", "prior2", "prior3"):
        sent = traffic.get(p, 0)
        allowed = success[p]
        blk = blocked[p]
        pct = f"{allowed / sent * 100:.1f}%" if sent > 0 else "N/A"
        share = f"{allowed / total_s * 100:.1f}%" if total_s > 0 else "N/A"
        reserved = r[f"{p}_reserved"]
        print(f"  {p:<12} {sent:>6} {allowed:>8} {blk:>8} {pct:>10} {share:>8} {reserved:>6} rpm")

    print(f"  {'-' * 75}")
    print(
        f"  {'TOTAL':<12} {total_sent:>6} {total_s:>8} {total_b:>8} "
        f"{f'{total_s / total_sent * 100:.1f}%' if total_sent > 0 else 'N/A':>10} "
        f"{'100%':>8} {MODEL_RPM:>6} rpm"
    )
    print(f"  Model capacity: {MODEL_RPM} RPM")
    print(f"  Over-capacity: {'YES' if total_s > MODEL_RPM else 'NO'} (allowed {total_s} vs cap {MODEL_RPM})")


SCENARIOS = [
    {
        "name": "S1: Low Traffic (30 per priority)",
        "description": "90 total requests, well below 180 RPM capacity. All should succeed.",
        "traffic": {"prior1": 30, "prior2": 30, "prior3": 30},
    },
    {
        "name": "S2: At Capacity (60 per priority)",
        "description": "180 total requests, exactly at model RPM.",
        "traffic": {"prior1": 60, "prior2": 60, "prior3": 60},
    },
    {
        "name": "S3: Over Capacity (200 each)",
        "description": "600 total requests. HTB should enforce model-wide cap at 180 RPM.",
        "traffic": {"prior1": 200, "prior2": 200, "prior3": 200},
    },
    {
        "name": "S4: prior1 Heavy (500/50/50)",
        "description": "prior1 sends 10x the traffic of others. prior1 capped at 90 RPM guaranteed.",
        "traffic": {"prior1": 500, "prior2": 50, "prior3": 50},
    },
    {
        "name": "S5: prior3 Heavy (50/50/500)",
        "description": "prior3 sends 10x the traffic. prior3 capped at 36 RPM guaranteed, prior1/prior2 unaffected.",
        "traffic": {"prior1": 50, "prior2": 50, "prior3": 500},
    },
    {
        "name": "S6: prior1 Only (500/0/0)",
        "description": "Only prior1 sends traffic. Even with 50% reservation, model cap (180) is the binding limit.",
        "traffic": {"prior1": 500, "prior2": 0, "prior3": 0},
    },
    {
        "name": "S7: Burst (1000/1000/1000)",
        "description": "3000 total requests, all priorities equal. Model cap enforced at 180 RPM.",
        "traffic": {"prior1": 1000, "prior2": 1000, "prior3": 1000},
    },
]


async def main():
    print("=" * 90)
    print("  HTB PRIORITY BEHAVIOUR SIMULATION")
    print(f"  Config: prior1=0.50 ({PRIOR1_RPM} rpm), prior2=0.30 ({PRIOR2_RPM} rpm), prior3=0.20 ({PRIOR3_RPM} rpm)")
    print(f"  Model: {MODEL} ({MODEL_RPM} RPM)")
    print(f"  Saturation threshold: {SATURATION_THRESHOLD:.0%}")
    print("=" * 90)

    results = []
    for scenario in SCENARIOS:
        r = await run_scenario(
            name=scenario["name"],
            description=scenario["description"],
            traffic=scenario["traffic"],
        )
        results.append(r)
        print_result_table(r)

    print(f"\n{'=' * 90}")
    print("  SUMMARY TABLE")
    print(f"{'=' * 90}")
    print(f"  {'Scenario':<48} {'P1':>5} {'P2':>5} {'P3':>5} {'Total':>6} {'Cap':>5} {'Over?':>6}")
    print(f"  {'-' * 83}")
    for r in results:
        over = "YES" if r["total_success"] > MODEL_RPM else "no"
        print(
            f"  {r['name']:<48} "
            f"{r['success']['prior1']:>5} "
            f"{r['success']['prior2']:>5} "
            f"{r['success']['prior3']:>5} "
            f"{r['total_success']:>6} "
            f"{MODEL_RPM:>5} "
            f"{over:>6}"
        )

    print(f"\n{'=' * 90}")
    print("  Done. All results above are from in-memory HTB fallback counters.")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(main())
