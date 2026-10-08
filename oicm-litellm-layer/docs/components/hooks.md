# Hooks (components #3 & #4)

LiteLLM callback hooks / plugins used by the proxy. These implement LiteLLM's
extension points (`CustomLogger`, pre-call hooks) without forking the proxy.

## Files

| File | Component | Purpose | Wired in prod? |
|------|-----------|---------|----------------|
| `hooks/vllm_param_injector.py` | #3 | `async_pre_call_hook` that relocates vLLM-specific params into `extra_body` | yes |
| `hooks/priority_bridge.py` | — | Bridges the HTB priority into `extra_body` fields (used with HTB rate limiting) | yes |
| `hooks/keda_metrics.py` | #4 | Emits the `ml_model_concurrent_requests` Prometheus gauge for KEDA autoscaling | no (present in the repo, not registered) |
| `hooks/__init__.py` | — | Package init (empty) |

## How they're wired in

Hooks are registered in the proxy config's `litellm_settings.callbacks` list.
The deployed list lives in the `litellm-config` ConfigMap at
`deploy/base/gateway/config/litellm-config.yaml`:

```yaml
litellm_settings:
  callbacks:
    - litellm_hooks.vllm_param_injector.vllm_param_injector
    - dynamic_rate_limiter_v3_htb
    - litellm_hooks.priority_bridge.priority_bridge
    - prometheus
    - litellm.router_utils.session_identity.resolver.proxy_handler_instance
```

The two `litellm_hooks.*` names are mounted from the `litellm-hooks` ConfigMap
(declared at `deploy/base/shared/litellm-hooks.yaml`) into `/app/litellm_hooks`
on the proxy pod. `dynamic_rate_limiter_v3_htb` is a LiteLLM in-tree hook (see
[OICM Custom Code](../oicm-slices.md)); `prometheus` and the session-identity
resolver are in-tree too.

!!! note
    `keda_metrics.py` is **not** in the `litellm-hooks` ConfigMap and **not** in
    the deployed callbacks list, so the `ml_model_concurrent_requests` gauge it
    defines is not currently emitted in prod. Register it in the ConfigMap and
    the callbacks list if KEDA autoscaling on that metric is needed again.

## Tests

- `tests/hooks/conftest.py`
- `tests/hooks/test_priority_bridge.py`

## Docs

- `docs/htb-rate-limiting/` — HTB rate limiting + priority bridge
- `docs/architecture/IMPLEMENTATION_PLAN.md` (components #3, #4)