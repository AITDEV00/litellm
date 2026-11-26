#!/usr/bin/env python
"""Live verify the HTB Lua window-value bug against LIVE Redis.

Loads the EXACT HTB_CHECK_AND_INCREMENT_SCRIPT, builds the exact keys/args
the same way htb_check_and_increment does, then runs it under two conditions:
  1. window value written via json.dumps(str(now))  -> CURRENT CODE (suspected bug)
  2. window value written via json.dumps(int(now))  -> PROPOSED FIX
Saves results for offline analysis.
"""
import asyncio
import json
import os
import time

import redis.asyncio as aioredis

from litellm.proxy.hooks.dynamic_rate_limiter_v3_htb import HTB_CHECK_AND_INCREMENT_SCRIPT

HOST = "127.0.0.1"
PORT = int(os.getenv("REDIS_PORT", "6379"))
PASSWORD = os.getenv("REDIS_PASSWORD", "")

MODEL = "zai-org/GLM-5.2-FP8"
PRIORITY = "prior1-1"
MODEL_LIMIT = 100
PRIORITY_LIMIT = 50
WINDOW_SIZE = 60


async def run_case(r, script, label, window_value):
    await r.flushdb()
    htb_hash = f"htb:{MODEL}"
    ps = PRIORITY
    # Sibling prior2 (guaranteed 30). Its demand window/counter are written by
    # _increment_demand_counter on the sibling pod with str(now) -> json.dumps.
    sib = "prior2-1"
    sib_demand_window_key = f"{{{htb_hash}}}:{sib}:demand:window"
    sib_demand_counter_key = f"{{{htb_hash}}}:{sib}:demand:requests"

    keys = [
        f"{{{htb_hash}}}:{ps}:window",
        f"{{{htb_hash}}}:{ps}:requests",
        f"{{{htb_hash}}}:window",
        f"{{{htb_hash}}}:requests",
        my_demand_window_key := f"{{{htb_hash}}}:{ps}:demand:window",
        my_demand_counter_key := f"{{{htb_hash}}}:{ps}:demand:requests",
        # sibling demand keys (KEYS[7], KEYS[8])
        sib_demand_window_key,
        sib_demand_counter_key,
    ]
    # num_siblings=1, sibling_guaranteed=30
    args = [PRIORITY_LIMIT, MODEL_LIMIT, WINDOW_SIZE, WINDOW_SIZE, 1, MODEL_LIMIT, 0, 30]

    now = int(time.time())
    # my own demand (not read by Lua, but written as code writes it)
    await r.set(my_demand_window_key, json.dumps(str(now)), ex=WINDOW_SIZE)
    await r.set(my_demand_counter_key, json.dumps(1), ex=WINDOW_SIZE)
    # SIBLING demand window: written by _increment_demand_counter -> json.dumps(window_value)
    await r.set(sib_demand_window_key, json.dumps(window_value), ex=WINDOW_SIZE)
    await r.set(sib_demand_counter_key, json.dumps(1), ex=WINDOW_SIZE)

    try:
        raw = await script(keys=keys, args=[str(a) for a in args])
        print(f"[{label}] Lua SUCCESS -> {raw}")
        return "OK"
    except Exception as e:  # noqa: BLE001
        print(f"[{label}] Lua FAILED -> {type(e).__name__}: {e}")
        return "FAILED"


async def main():
    r = aioredis.from_url(f"redis://:{PASSWORD}@{HOST}:{PORT}", decode_responses=True)
    await r.ping()
    print("Connected to live litellm-redis")
    script = r.register_script(HTB_CHECK_AND_INCREMENT_SCRIPT)

    print("=== CURRENT CODE: window written as json.dumps(str(now)) ===")
    res1 = await run_case(r, script, "str(now)", str(int(time.time())))

    print("=== PROPOSED FIX: window written as json.dumps(int(now)) ===")
    res2 = await run_case(r, script, "int(now)", int(time.time()))

    print("\n=== RESULT ===")
    print(f"  str(now)  -> Lua: {res1}")
    print(f"  int(now)  -> Lua: {res2}")
    with open("02-lua-result.txt", "w") as f:
        f.write(f"str(now) -> {res1}\nint(now) -> {res2}\n")
    await r.aclose()


if __name__ == "__main__":
    asyncio.run(main())