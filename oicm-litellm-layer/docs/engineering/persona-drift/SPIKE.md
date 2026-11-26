# Persona Drift / Language-Confusion Drift — Spike Note

Status: not started. This file is a placeholder that records the finding and the
available injection points so a future spike can pick it up without re-doing the
research.

## The finding

ContextEcho (Accenture, arXiv:2605.24279, 2026) measures whether a model's trained
Assistant persona survives long agentic-coding sessions (thousands of tool-using
turns). It is a 25-probe identity suite that snapshots a real Claude Code or Codex
CLI session, forks the conversation, and probes a chat-completions target on the
fork. Reported results across 23 frontier models from 10 orgs, on three anonymized
sessions (445-1,242 user turns; 3,746-9,643 agent steps):

| # | Finding | Takeaway |
|---|---------|----------|
| 1 | Drift is general, not family-specific | Persona drift appears across orgs, not just one model family |
| 2 | Compaction does not reliably reset it | In-session context compaction fails to restore the trained register |
| 3 | A single-shot anchor restores the persona | One ~110-token anchor turn recovers the trained register across measured targets, persisting 20+ turns |
| 4 | Downstream effects are mode-dependent | Drift can aid tool-using continuation, but in tool-free chat it breaks format contracts and inflates output length |

The placement result matters most for us: the paper found the user-turn placement is
consistently better than the system-prompt placement, hypothesizing that a reminder
in a recent user-turn exchange recruits the same register-conditioning machinery the
long session prefix recruits, while a system-prompt reminder is more easily
out-prioritized by the bulk of recent context.

This is the same family of failure we have been calling language confusion drift
(the model slipping out of the requested language or register mid-session), so the
anchor mechanism is worth testing against that symptom too.

Repo: https://github.com/Accenture/ContextEcho

## Why this matters for the OICM gateway

The gateway sits in front of every LLM call, so it is the natural place to re-assert
an anchor without touching each client. LiteLLM exposes hooks that run before the
provider call, so this can be done config-side with no fork.

## Injection points available (research done, not implemented)

| # | Mechanism | Location | Placement | Notes |
|---|-----------|----------|-----------|-------|
| 1 | Custom callback `async_pre_call_hook` | `litellm/proxy/hooks/*`, wired via `litellm_settings.callbacks` | Can append to last user turn or insert a trailing message | Best fit. Fires for every model and call type. Our layer already uses this pattern in `hooks/vllm_param_injector.py`, so a sibling `hooks/persona_anchor.py` is one config line |
| 2 | Per-model `litellm_system_prompt` | `litellm/main.py:5249` -> `add_system_prompt_to_messages` in `litellm/litellm_core_utils/prompt_templates/common_utils.py:1843` | System prompt only | Declarative via the model list `litellm_params`, but it is the weaker placement and static per model |
| 3 | Guardrail `inject_system_message` | `litellm/proxy/guardrails/guardrail_hooks/lakera_ai_v2.py`, `litellm/types/guardrails.py:604` | System message, only when flagged | Reactive, not a per-request anchor. Existing precedent for gateway-appended system messages |
| 4 | Split-softmax | serving side (vLLM), not LiteLLM | n/a | Amplifies system-prompt attention at inference time. A serving change, not a hook |

Reference implementations of the hook interface:
`litellm/proxy/hooks/prompt_injection_detection.py` and
`litellm/proxy/hooks/litellm_skills/main.py`.

## Open questions for the spike

- Which anchor text actually reduces our language-confusion drift, and at what
  token cost. The paper's anchor is ~110 tokens and persona-shaped; ours may need to
  be language-shaped.
- Placement: last user turn vs trailing system/developer message, tested against our
  own symptom rather than the paper's persona probes.
- Idempotency: the client owns message history, so an anchor injected per request is
  not sticky. Detect an existing anchor and skip to avoid stacking.
- Prompt-cache impact: an anchor appended near the end of every request changes the
  cache key, so hit rates drop. Worth measuring.
- Anthropic Messages API vs OpenAI chat completions: the system prompt lives in a
  top-level `system` field for the former and in the messages array for the latter,
  so the hook needs both paths.
- How to measure the symptom locally: a small probe set that would fail before the
  anchor and pass after, in the spirit of the benchmark's snapshot-then-probe design.

## Suggested first step

Write a minimal `hooks/persona_anchor.py` for the OICM layer (last-user-turn
placement, Anthropic and OpenAI paths, idempotency guard, config toggle) plus a
regression test, then run it against a local proxy with a small probe set to see if
the symptom moves. Keep it behind a config flag so it can be disabled without a
redeploy.

## Caveat

This is a lightweight mitigation, not a fix. The paper itself notes that success on
low-drift targets should not be read as evidence of drift, and that drift can
sometimes help tool-using continuation while hurting tool-free chat. Treat the
anchor as a tunable, measurable nudge, not a guarantee.
