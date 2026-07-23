from controller.pricing.matchers import (
    exact_match,
    fuzzy_match,
    structured_match,
    substring_match,
)
from controller.pricing.models import PricingEntry
from controller.pricing.normalizer import normalize_model_name


def _make_entry(key: str, input_cost: float = 1e-07, output_cost: float = 2e-07) -> PricingEntry:
    return PricingEntry(
        key=key,
        litellm_provider="test",
        mode="chat",
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
        has_pricing=True,
        source_url="",
    )


def _build_index(entries: dict[str, PricingEntry]) -> dict[str, PricingEntry]:
    return entries


class TestExactMatcher:
    def test_exact_match_after_normalization(self):
        entry = _make_entry("deepseek-chat")
        index = _build_index({"deepseek-chat": entry})
        result = exact_match("deepseek-chat", index)
        assert len(result) == 1
        assert result[0].json_key == "deepseek-chat"
        assert result[0].score == 1.0
        assert result[0].matcher_name == "exact"

    def test_no_match(self):
        index = _build_index({"deepseek-chat": _make_entry("deepseek-chat")})
        result = exact_match("gpt-4o", index)
        assert len(result) == 0

    def test_empty_model(self):
        index = _build_index({"deepseek-chat": _make_entry("deepseek-chat")})
        result = exact_match("", index)
        assert len(result) == 0


class TestStructuredMatcher:
    def test_exact_family_and_param_match(self):
        index = _build_index({
            "openrouter/qwen/qwen3-32b": _make_entry("openrouter/qwen/qwen3-32b"),
        })
        result = structured_match("qwen3-32b", index)
        assert len(result) == 1
        assert result[0].score == 0.95
        assert result[0].matcher_name == "structured"

    def test_family_only_match(self):
        index = _build_index({
            "openrouter/qwen/qwen3-coder": _make_entry("openrouter/qwen/qwen3-coder"),
        })
        result = structured_match("qwen3-coder", index)
        assert len(result) == 1
        assert result[0].score == 0.70

    def test_prefers_exact_param_over_family_only(self):
        index = _build_index({
            "openrouter/qwen/qwen3-32b": _make_entry("openrouter/qwen/qwen3-32b"),
            "openrouter/qwen/qwen3-72b": _make_entry("openrouter/qwen/qwen3-72b"),
        })
        result = structured_match("qwen3-32b", index)
        assert len(result) == 1
        assert result[0].score == 0.95

    def test_no_match_different_family(self):
        index = _build_index({
            "deepseek-chat": _make_entry("deepseek-chat"),
        })
        result = structured_match("qwen3-32b", index)
        assert len(result) == 0

    def test_empty_model(self):
        index = _build_index({"deepseek-chat": _make_entry("deepseek-chat")})
        result = structured_match("", index)
        assert len(result) == 0


class TestFuzzyMatcher:
    def test_high_similarity_match(self):
        index = _build_index({
            "deepseek-chat": _make_entry("deepseek-chat"),
        })
        result = fuzzy_match("deepseek-chat", index)
        assert len(result) == 1
        assert result[0].score >= 0.80
        assert result[0].matcher_name == "fuzzy"

    def test_low_similarity_filtered(self):
        index = _build_index({
            "amazon-titan-embed-text": _make_entry("amazon-titan-embed-text"),
        })
        result = fuzzy_match("deepseek-chat", index)
        assert len(result) == 0

    def test_token_prefilter_excludes_no_overlap(self):
        index = _build_index({
            "gpt-4o": _make_entry("gpt-4o"),
        })
        result = fuzzy_match("deepseek-chat", index)
        assert len(result) == 0

    def test_empty_model(self):
        index = _build_index({"deepseek-chat": _make_entry("deepseek-chat")})
        result = fuzzy_match("", index)
        assert len(result) == 0


class TestSubstringMatcher:
    def test_model_is_substring_of_key(self):
        index = _build_index({
            "deepseek/deepseek-v3": _make_entry("deepseek/deepseek-v3"),
        })
        result = substring_match("deepseek-v3", index)
        assert len(result) == 1
        assert result[0].score == 0.85
        assert result[0].matcher_name == "substring"

    def test_key_is_substring_of_model(self):
        index = _build_index({
            "deepseek-v3": _make_entry("deepseek-v3"),
        })
        result = substring_match("deepseek-v3.2", index)
        assert len(result) == 1

    def test_no_substring(self):
        index = _build_index({
            "gpt-4o": _make_entry("gpt-4o"),
        })
        result = substring_match("deepseek-v3", index)
        assert len(result) == 0

    def test_empty_model(self):
        index = _build_index({"deepseek-v3": _make_entry("deepseek-v3")})
        result = substring_match("", index)
        assert len(result) == 0
