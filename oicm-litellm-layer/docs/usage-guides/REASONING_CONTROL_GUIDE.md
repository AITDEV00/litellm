# Reasoning control on the gateway

How `reasoning_effort` behaves per model on `litellm.ecouncil.ae`, how to turn
thinking off, and where the behavior is implemented in SGLang. Verified against
the SGLang source at `/home/jyao/ADEO/mlops/sglang` (v0.5.20), the SGLang
cookbook, and live probes through the gateway.

## Summary

| Model | Tiers honored | Default | Can thinking be disabled |
|---|---|---|---|
| `moonshotai/Kimi-K3` | low, high, max | max | Yes, `reasoning_effort: "none"` |
| `zai-org/GLM-5.3-Flash` | low, high | max | Yes, `reasoning_effort: "none"` |
| `zai-org/GLM-5.3` | low, high | max | No, always-on |
| `Qwen/Qwen3.8-Flash-Next-FP8` | low, medium, xhigh | xhigh | Yes, `reasoning_effort: "none"` |

The tier set is not a gateway-wide enum. SGLang forwards `reasoning_effort`
straight into the checkpoint's chat template, so each model implements its own
accepted set. A tier that a sibling model accepts can be ignored or rejected
here.

## Turning thinking off

`reasoning_effort: "none"` disables thinking on every toggle-based model. The
reliable way to confirm it actually disabled, rather than stripped the block, is
the usage block: `completion_tokens_details` comes back as `{}` with no
`reasoning_tokens` key, versus `{"reasoning_tokens": N}` when thinking ran.

GLM-5.3 (non-Flash) is the exception. It cannot be disabled by any request
parameter. Sending `none` or `chat_template_kwargs: {"enable_thinking": false}`
makes it spend its whole token budget on reasoning and return `content: null`,
the opposite of disabling.

## Per-model notes

Kimi K3 always thinks. `low`, `high`, and `max` are honored and default to
`max`. Other tiers are accepted without error but are not honored, so they
fall back to the template default.

GLM-5.3-Flash honors `low` and `high`, both close to the reasoning floor, so
they produce only a few reasoning tokens. Tiers above `high` fall back to
`max`. The cookbook documents disabling with `chat_template_kwargs:
{"thinking": false}`, but on this gateway the top-level `reasoning_effort:
"none"` is the reliable switch.

GLM-5.3 (non-Flash) honors `low` and `high`. Everything else, including
`medium`, `xhigh`, `max`, and `none`, falls through to `max`. It is always-on.

Qwen3.8-Flash-Next honors `low`, `medium`, and `xhigh` (default). Sending
`high`, `max`, or `minimal` returns HTTP 400 with `Unexpected reasoning effort
{x}. Supported types are xhigh (default), medium, and low.` The SGLang cookbook
claims thinking cannot be turned off, but the gateway honors `reasoning_effort:
"none"` and stops it.

## Mechanism

`reasoning_effort` is forwarded to the chat template as an
`extra_template_kwargs` entry (`serving_chat.py`, around line 1510). Kimi
additionally maps it to the template's `thinking_effort` kwarg and accepts only
`low`, `high`, and `max`, warning and using the encoder default otherwise.

The universal off switch is in `protocol.py`, `normalize_reasoning_inputs`
(around line 1046). Any `reasoning_effort` value other than `none` sets
`chat_template_kwargs.thinking` and `.enable_thinking` to true, and `none` sets
both to false. Both keys are set because families differ, `thinking` for
DeepSeek and Kimi, `enable_thinking` for Qwen and GLM. The write uses
`setdefault`, so an explicit `chat_template_kwargs` value from the caller wins
over the top-level effort.

GLM-5.3 (non-Flash) is detected as always-on in `template_detection.py`.
`_is_glm53` requires the template to contain `Reasoning Effort:` and to not
contain `enable_thinking`, and resolves to
`ReasoningToggleConfig(special_case="always")`. `_is_glm45` requires the
`enable_thinking` toggle. That is why the `enable_thinking: false` key is a
no-op on GLM-5.3: its template uses a `Reasoning Effort:` header instead of a
boolean toggle. `_get_reasoning_from_request` returns true unconditionally for
always-on models, and `apply_reasoning_enabled` raises if a caller tries to
disable one.

## Setting a gateway default

An operator-level default lives in `litellm_params.reasoning_effort` on the
deployment, which the Router merges into every request that carries no effort of
its own. Use `scripts/set_reasoning_effort_default.sh` to set it. The patch
carries only that key, so it is reconcile-safe: the OICM controller patches the
same way and never sends the key, and the merge preserves it.

Note that `model_info.default_reasoning_effort` is not an injection mechanism.
Its only consumer is the OpenAI GPT-5 sampling-param guard, and it cannot hold
`max`.

## Reproducing

`scripts/probe_reasoning.py` runs the probes. Every request carries a unique
nonce so the LiteLLM response cache always misses, and a repeated response id is
flagged as a cache hit, so a cached answer cannot be mistaken for a fresh
generation.

```bash
export PROXY_BASE_URL="https://litellm.ecouncil.ae"
export LITELLM_API_KEY="sk-..."

python3 scripts/probe_reasoning.py --list
python3 scripts/probe_reasoning.py --label kimi --repeat 3 --summary
python3 scripts/probe_reasoning.py --probe 'tag|model|{"reasoning_effort":"low"}'
```

Two cautions when reading results. LiteLLM response caching is on, so any probe
that reuses a prompt without a nonce will serve a stale body. And at a low
`max_tokens` a thinking model burns the whole budget on reasoning and returns
`content: null`, so empty content is itself a thinking-on signal.
