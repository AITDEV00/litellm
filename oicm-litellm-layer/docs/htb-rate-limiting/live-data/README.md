# HTB Lua window-value bug: live validation artifacts

Artifacts from reproducing and validating the Redis Lua `user_script:66` bug
against a LIVE `litellm-redis` instance (multi-instance deployment).

## What the bug was

`_increment_demand_counter` in
`litellm/proxy/hooks/dynamic_rate_limiter_v3_htb.py` wrote the demand-window
timestamp as `value=str(now)`. The Redis cache layer serializes values with
`json.dumps`, producing a JSON-quoted string (`"1728..."`).

The HTB Lua script reads a sibling pod's demand window via
`redis.call('GET', ...)` and parses it with `tonumber(window_start)`. `tonumber`
of a JSON-quoted string returns `nil`, crashing the script at
`@user_script:66` ("attempt to perform arithmetic on a nil value") and silently
degrading multi-instance HTB to the in-memory fallback.

## Fix

Store the window value as the raw int: `value=str(now)` -> `value=now`. `int`
values survive `json.dumps` unquoted, so Lua `tonumber` parses them.

## How to reproduce

Requires access to a live Redis (e.g. via port-forward) and the `REDIS_PASSWORD`
environment variable.

```bash
export REDIS_PASSWORD=<redis-password>
python 01-verify-lua-bug.py
```

Output (`02-lua-result.txt`) shows `str(now)` failing the exact Lua parse while
`int(now)` succeeds. A focused regression test that runs the real Lua `tonumber`
against a real Redis lives in
`tests/test_litellm/proxy/hooks/test_priority_reservation_adeo.py`
(`test_demand_window_value_is_lua_tonumber_parseable`).