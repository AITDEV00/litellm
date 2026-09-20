# Hooks (components #3 & #4)

LiteLLM callback hooks / plugins used by the proxy. These implement LiteLLM's
extension points (`CustomLogger`, pre-call hooks) without forking the proxy.

## Files

| File | Component | Purpose |
|------|-----------|---------|
| `hooks/vllm_param_injector.py` | #3 | `async_pre_call_hook` that relocates vLLM-specific params into `extra_body` |
| `hooks/keda_metrics.py` | #4 | Emits `ml_model_concurrent_requests` Prometheus gauge for KEDA autoscaling |
| `hooks/priority_bridge.py` | — | Bridges priority into the rate limiter (used with HTB rate limiting) |
| `hooks/__init__.py` | — | Package init |

## How they're wired in

Hooks are registered in the proxy config's `litellm_settings.callbacks` list.
In production that list lives in the inline `config.yaml` inside
`deploy/prod/litellm-proxy.yaml`:

```yaml
litellm_settings:
  callbacks:
    - litellm_hooks.vllm_param_injector.vllm_param_injector
    - dynamic_rate_limiter_v3
    - litellm_hooks.priority_bridge.priority_bridge
    - prometheus
```

> Note the deployed hook names are prefixed with `litellm_hooks.` and are
> mounted from the `litellm-hooks` ConfigMap into `/app/litellm_hooks` on the
> proxy pod.

## Tests

- `tests/hooks/conftest.py`
- `tests/hooks/test_priority_bridge.py`

## Docs

- `docs/htb-rate-limiting/` — HTB rate limiting + priority bridge
- `docs/architecture/IMPLEMENTATION_PLAN.md` (components #3, #4)