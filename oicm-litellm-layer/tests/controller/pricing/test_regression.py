import asyncio
from pathlib import Path

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
