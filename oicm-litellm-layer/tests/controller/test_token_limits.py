"""Regression tests for the token-budget split the controller stamps.

A client that adopts `max_output_tokens` as its request default overflows
`prompt + max_tokens <= max_model_len` when every advertised field equals the
window, so these tests pin the invariant the stamp exists to guarantee:
output strictly below the window, input + output never above it, and the
legacy `max_tokens` pinned to the same budget so the gateway's discovery
refresh cannot reintroduce the window through it.
"""

from controller.token_limits import (
    SCALED_OUTPUT_DIVISOR,
    TEMPLATE_RESERVE_TOKENS,
    token_budgets,
)


def test_large_window_gets_the_full_chat_budget():
    budgets = token_budgets(524288, "chat")
    assert budgets == {
        "max_tokens": 64000,
        "max_input_tokens": 524288 - 64000 - TEMPLATE_RESERVE_TOKENS,
        "max_output_tokens": 64000,
        "context_length": 524288,
    }


def test_small_window_scales_the_budget_down():
    # A flat 64k budget on a 64k window would leave zero input, so the budget
    # divides the window instead.
    budgets = token_budgets(65536, "chat")
    assert budgets["max_output_tokens"] == 65536 // 8
    assert budgets["max_input_tokens"] == 65536 - 65536 // 8 - TEMPLATE_RESERVE_TOKENS
    assert budgets["context_length"] == 65536


def test_input_plus_output_stays_strictly_below_the_window():
    # The backend folds chat-template overhead (~32 tokens, measured live)
    # into prompt_tokens, so a prompt exactly filling the advertised input
    # budget still overflows by the template's size. The advertised sum must
    # keep the reserve below the window instead of sitting at its boundary.
    for window in (65536, 131072, 262144, 524288, 1000000):
        budgets = token_budgets(window, "chat")
        assert budgets["max_output_tokens"] < window
        assert (
            budgets["max_input_tokens"] + budgets["max_output_tokens"]
            <= window - TEMPLATE_RESERVE_TOKENS
        )
        assert budgets["context_length"] == window


def test_output_is_always_strictly_below_the_window():
    for window in (1 << 16, 1 << 17, 1 << 18, 131072, 262144, 524288):
        budgets = token_budgets(window, "chat")
        assert budgets["max_output_tokens"] < window
        assert budgets["max_input_tokens"] > 0


def test_budget_never_exceeds_the_window_on_odd_sizes():
    # A window too small to divide claims nothing at all: a zero-token
    # completion budget would put max_tokens=0 on the wire for some clients,
    # and reserving the template can starve the input on small windows too.
    assert token_budgets(7, "chat") == {}
    assert token_budgets(SCALED_OUTPUT_DIVISOR - 1, "chat") == {}
    assert token_budgets(SCALED_OUTPUT_DIVISOR, "chat") == {}


def test_non_chat_modes_carry_no_budget():
    # Nothing this controller measures says what a transcription or TTS output
    # budget should be, so those modes must not claim one.
    assert token_budgets(131072, "audio_transcription") == {}
    assert token_budgets(131072, "text_to_speech") == {}
    assert token_budgets(131072, "embedding") == {}


def test_unknown_window_claims_nothing():
    # A probe failure or a backend that advertises no max_model_len must leave
    # the token fields alone; absence is safe because the gateway's own
    # discovery then fills them (as before), and no false budget is invented.
    assert token_budgets(None, "chat") == {}
    assert token_budgets(0, "chat") == {}
    assert token_budgets(-5, "chat") == {}
    assert token_budgets(True, "chat") == {}


def test_max_tokens_is_pinned_to_the_output_budget():
    # The gateway's discovery refresh fills an unset max_tokens with the
    # context window, and some clients read max_tokens as the output limit
    # when max_output_tokens is absent, so it must equal the budget, never
    # the window.
    budgets = token_budgets(524288, "chat")
    assert budgets["max_tokens"] == budgets["max_output_tokens"]
