"""Token-limit metadata the controller stamps so the gateway advertises a
usable completion budget instead of the raw context window.

The gateway's own model-info refresh probes each OpenAI-compatible
deployment's ``/v1/models``, reads ``max_model_len``, and fills
``max_tokens``/``max_input_tokens``/``max_output_tokens`` with it for any
``model_info`` key a stored row leaves unset. With all three equal to the
window, a client that adopts ``max_output_tokens`` as its request default
overflows ``prompt + max_tokens <= max_model_len`` on every request, so a
one-word prompt 400s.

A stored ``model_info`` key wins per-key over the discovered value, so the
controller pre-empts the fill: it stores a real completion budget, derives the
input budget from it, and also pins ``max_tokens`` (LiteLLM's legacy output
field, which some client code paths fall back to) to the same budget. Leaving
``max_tokens`` unset would let the gateway's refresh stamp the window back
into it, which is how ``524288`` reappears in listings that were patched
without it.
"""

from typing import Final, Mapping, Optional

# The completion budget for a window large enough to spare one. Reproduces the
# split the OAI-compatible extension ships (512000/64000) and keeps the
# requested budget strictly below the window, leaving headroom for
# chat-template overhead (~32 tokens) and client-side token-estimate error.
CHAT_OUTPUT_TOKENS: Final[int] = 64000

# Below this window a flat 64k budget would leave too little input, so the
# budget scales down with the window instead.
SCALED_OUTPUT_DIVISOR: Final[int] = 8

# The backend folds chat-template overhead (~32 tokens, measured live) into
# prompt_tokens, so a prompt exactly filling the advertised input budget
# still overflows the window by the template's size. Reserving it keeps
# input + output strictly below the window instead of at its exact boundary.
TEMPLATE_RESERVE_TOKENS: Final[int] = 64


def token_budgets(max_model_len: Optional[int], mode: str) -> Mapping[str, int]:
    """The ``model_info`` token keys to stamp for a discovered deployment.

    Returns an empty mapping when there is nothing safe to claim: an unknown
    window (probe failed, or a backend that advertises no ``max_model_len``)
    leaves the gateway's own discovery in charge, and non-chat modes never
    carry a completion budget because nothing this controller measures says
    what one should be.
    """
    if mode != "chat":
        return {}
    if not isinstance(max_model_len, int) or isinstance(max_model_len, bool) or max_model_len <= 0:
        return {}
    output = min(CHAT_OUTPUT_TOKENS, max_model_len // SCALED_OUTPUT_DIVISOR)
    input_budget = max_model_len - output - TEMPLATE_RESERVE_TOKENS
    if output <= 0 or input_budget <= 0:
        # A window too small to split would advertise a zero-token completion
        # budget (which a client may put on the wire as max_tokens=0, an
        # invalid request) or no input room at all. Claiming nothing is the
        # safe answer: clients fall back to their own conservative, capped
        # guess.
        return {}
    return {
        "max_tokens": output,
        "max_input_tokens": input_budget,
        "max_output_tokens": output,
        "context_length": max_model_len,
    }
