import asyncio
from pathlib import Path

from controller.pricing.resolver import PricingResolver
from controller.pricing.source import PricingSource

FIXTURE_PATH = Path(__file__).parent / "pricing_fixture.json"


def _make_resolver() -> PricingResolver:
    source = PricingSource(json_path=str(FIXTURE_PATH))
    return PricingResolver(source)


class TestResolverResolve:
    def test_exact_match_for_bare_key(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("deepseek-chat"))
        assert result is not None
        assert result.input_cost_per_token == 2.8e-07
        assert result.output_cost_per_token == 4.2e-07
        assert "deepseek-chat" in result.matched_keys

    def test_resolves_huggingface_name_to_structured_match(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("Qwen/Qwen3-32B-Instruct"))
        assert result is not None
        assert result.input_cost_per_token > 0
        assert "qwen.qwen3-32b-v1:0" in result.matched_keys or any(
            "qwen3-32b" in k or "32b" in k for k in result.matched_keys
        )

    def test_resolves_prefixed_name(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("deepseek-ai/DeepSeek-V3"))
        assert result is not None
        assert result.input_cost_per_token > 0

    def test_resolves_bedrock_dotted_key(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("qwen.qwen3-coder-30b-a3b-v1:0"))
        assert result is not None
        assert result.input_cost_per_token == 1.5e-07

    def test_no_match_returns_none(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("some-completely-unknown-model"))
        assert result is None

    def test_empty_model_id_returns_none(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve(""))
        assert result is None

    def test_does_not_match_no_pricing_entry(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("Qwen/Qwen2.5-72B-Instruct-Turbo"))
        if result is not None:
            assert "together_ai/Qwen/Qwen2.5-72B-Instruct-Turbo" not in result.matched_keys

    def test_does_not_match_image_generation(self):
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("dall-e-2"))
        assert result is None


class TestResolverDisabled:
    def test_returns_none_when_disabled(self, monkeypatch):
        import controller.pricing.resolver as resolver_module

        monkeypatch.setattr(resolver_module, "PRICING_ENABLED", False)
        resolver = _make_resolver()
        result = asyncio.run(resolver.resolve("deepseek-chat"))
        assert result is None
