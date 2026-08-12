import asyncio
from pathlib import Path

from controller.pricing.matchers import substring_match
from controller.pricing.models import PricingEntry
from controller.pricing.resolver import PricingResolver
from controller.pricing.source import PricingSource

FIXTURE_PATH = Path(__file__).parent / "pricing_fixture.json"


def _make_resolver() -> PricingResolver:
    source = PricingSource(json_path=str(FIXTURE_PATH))
    return PricingResolver(source)


class TestRegression32BNot72B:
    def test_32b_model_does_not_get_72b_price_when_32b_exists(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("Qwen/Qwen2.5-32B-Instruct"))
        assert result is not None
        assert "together_ai/Qwen/Qwen2.5-72B-Instruct-Turbo" not in result.matched_keys


class TestRegressionNoMatchStaysZero:
    def test_unmatched_model_returns_none_not_random_price(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("totally-unknown-model-xyz"))
        assert result is None


class TestRegressionEmptyJsonNoCrash:
    def test_empty_json_returns_none(self):
        source = PricingSource(json_path="/nonexistent/path.json", base_url="")
        resolver = PricingResolver(source)
        result = asyncio.run(resolver.resolve("deepseek-chat"))
        assert result is None


class TestRegressionNoPricingEntryExcluded:
    def test_no_pricing_entry_does_not_contribute_zero(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("Qwen/Qwen2.5-72B-Instruct-Turbo"))
        if result is not None:
            assert "together_ai/Qwen/Qwen2.5-72B-Instruct-Turbo" not in result.matched_keys


class TestRegressionTieredPricing:
    def test_tiered_entry_contributes_first_tier_not_zero(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("dashscope/qwen-flash"))
        assert result is not None
        assert result.input_cost_per_token == 5e-08
        assert result.output_cost_per_token == 4e-07


class TestRegressionSubstringMinLength:
    def test_short_normalized_key_does_not_substring_match(self):
        entry = PricingEntry(
            key="dashscope/qwen-turbo",
            litellm_provider="dashscope",
            mode="chat",
            input_cost_per_token=1e-07,
            output_cost_per_token=2e-07,
            has_pricing=True,
            source_url="",
        )
        index = {"qwen": entry}
        result = substring_match("qwen3.5-0.8b", index)
        assert len(result) == 0

    def test_two_char_normalized_key_does_not_substring_match(self):
        entry = PricingEntry(
            key="deepseek.v3-v1:0",
            litellm_provider="bedrock_converse",
            mode="chat",
            input_cost_per_token=5e-07,
            output_cost_per_token=1e-06,
            has_pricing=True,
            source_url="",
        )
        index = {"v3": entry}
        result = substring_match("whisper-large-v3", index)
        assert len(result) == 0

    def test_long_enough_key_still_substring_matches(self):
        entry = PricingEntry(
            key="deepseek/deepseek-v3",
            litellm_provider="deepseek",
            mode="chat",
            input_cost_per_token=2.8e-07,
            output_cost_per_token=4.2e-07,
            has_pricing=True,
            source_url="",
        )
        index = {"deepseek-v3": entry}
        result = substring_match("deepseek-v3.2", index)
        assert len(result) == 1
