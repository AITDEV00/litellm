import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from controller.pricing.source import (
    PricingSource,
    _build_entry,
    _build_index,
    _load_from_file,
)
from controller.pricing.models import PricingEntry

FIXTURE_PATH = Path(__file__).parent / "pricing_fixture.json"


class TestBuildEntry:
    def test_excludes_sample_spec(self):
        assert _build_entry("sample_spec", {"mode": "chat"}) is None

    def test_excludes_fallback_generalizations(self):
        assert _build_entry("fallback_generalizations", {"rules": []}) is None

    def test_excludes_image_generation_mode(self):
        entry = _build_entry(
            "1024-x-1024/dall-e-2",
            {"litellm_provider": "openai", "mode": "image_generation"},
        )
        assert entry is None

    def test_excludes_entry_with_no_pricing(self):
        entry = _build_entry(
            "together_ai/Qwen/Qwen2.5-72B-Instruct-Turbo",
            {"litellm_provider": "together_ai", "mode": "chat"},
        )
        assert entry is not None
        assert entry.has_pricing is False

    def test_includes_embedding_with_zero_output_cost(self):
        entry = _build_entry(
            "amazon.titan-embed-text-v1",
            {
                "litellm_provider": "bedrock",
                "mode": "embedding",
                "input_cost_per_token": 1e-07,
                "output_cost_per_token": 0.0,
            },
        )
        assert entry is not None
        assert entry.has_pricing is True
        assert entry.output_cost_per_token == 0.0
        assert entry.mode == "embedding"

    def test_extracts_tiered_pricing_first_tier(self):
        entry = _build_entry(
            "dashscope/qwen-flash",
            {
                "litellm_provider": "dashscope",
                "mode": "chat",
                "tiered_pricing": [
                    {
                        "input_cost_per_token": 5e-08,
                        "output_cost_per_token": 4e-07,
                        "range": [0, 256000.0],
                    },
                    {
                        "input_cost_per_token": 2.5e-07,
                        "output_cost_per_token": 2e-06,
                        "range": [256000.0, 1000000.0],
                    },
                ],
            },
        )
        assert entry is not None
        assert entry.has_pricing is True
        assert entry.input_cost_per_token == 5e-08
        assert entry.output_cost_per_token == 4e-07

    def test_flat_pricing(self):
        entry = _build_entry(
            "deepseek-chat",
            {
                "litellm_provider": "deepseek",
                "mode": "chat",
                "input_cost_per_token": 2.8e-07,
                "output_cost_per_token": 4.2e-07,
            },
        )
        assert entry is not None
        assert entry.input_cost_per_token == 2.8e-07
        assert entry.output_cost_per_token == 4.2e-07


class TestBuildIndex:
    def _load_fixture(self) -> dict:
        return json.loads(FIXTURE_PATH.read_text())

    def test_filters_reserved_keys(self):
        index = _build_index(self._load_fixture())
        assert "sample_spec" not in index.entries
        assert "fallback_generalizations" not in index.entries

    def test_filters_image_generation(self):
        index = _build_index(self._load_fixture())
        assert "1024-x-1024/dall-e-2" not in index.entries

    def test_excludes_no_pricing_entries(self):
        index = _build_index(self._load_fixture())
        assert "together_ai/Qwen/Qwen2.5-72B-Instruct-Turbo" not in index.entries
        assert index.skipped_no_pricing == 2

    def test_includes_tiered_pricing(self):
        index = _build_index(self._load_fixture())
        entry = index.entries.get("dashscope/qwen-flash")
        assert entry is not None
        assert entry.input_cost_per_token == 5e-08

    def test_includes_embedding(self):
        index = _build_index(self._load_fixture())
        assert "amazon.titan-embed-text-v1" in index.entries

    def test_builds_normalized_key_map(self):
        index = _build_index(self._load_fixture())
        assert "deepseek-chat" in index.by_normalized_key
        assert "qwen3-coder" in index.by_normalized_key

    def test_empty_input(self):
        index = _build_index({})
        assert len(index) == 0
        assert bool(index) is False


class TestLoadFromFile:
    def test_loads_valid_json(self):
        raw = _load_from_file(str(FIXTURE_PATH))
        assert "deepseek-chat" in raw
        assert "sample_spec" in raw

    def test_raises_on_missing_file(self):
        try:
            _load_from_file("/nonexistent/path.json")
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError:
            pass


class TestPricingSource:
    def test_loads_from_file(self):
        source = PricingSource(json_path=str(FIXTURE_PATH))
        index = asyncio.run(source.get_index())
        assert len(index) > 0
        assert "deepseek-chat" in index.entries

    def test_fallback_to_empty_on_missing_file(self):
        source = PricingSource(json_path="/nonexistent/path.json", base_url="")
        index = asyncio.run(source.get_index())
        assert len(index) == 0

    def test_serves_stale_on_reload_failure(self):
        source = PricingSource(json_path=str(FIXTURE_PATH))
        first = asyncio.run(source.get_index())
        assert len(first) > 0

        source._json_path = "/nonexistent/path.json"
        source._last_load = 0.0
        second = asyncio.run(source.get_index())
        assert second is first
