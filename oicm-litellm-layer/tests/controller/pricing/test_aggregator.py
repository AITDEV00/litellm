from controller.pricing.aggregator import aggregate
from controller.pricing.models import MatcherCandidate


def _candidate(key: str, score: float, input_cost: float = 1e-07, output_cost: float = 2e-07, matcher: str = "fuzzy") -> MatcherCandidate:
    return MatcherCandidate(
        json_key=key,
        input_cost_per_token=input_cost,
        output_cost_per_token=output_cost,
        score=score,
        matcher_name=matcher,
    )


class TestAggregate:
    def test_empty_candidates_returns_none(self):
        assert aggregate([], "model-x") is None

    def test_single_candidate_returns_directly(self):
        c = _candidate("deepseek-chat", 1.0, matcher="exact")
        result = aggregate([c], "deepseek-chat")
        assert result is not None
        assert result.input_cost_per_token == 1e-07
        assert result.output_cost_per_token == 2e-07
        assert result.matched_keys == ("deepseek-chat",)
        assert result.aggregate_score == 1.0
        assert result.strategy == "exact"

    def test_all_below_threshold_returns_none(self):
        c = _candidate("some-model", 0.50)
        assert aggregate([c], "model-x", threshold=0.80) is None

    def test_multiple_candidates_weighted_average(self):
        c1 = _candidate("qwen3-32b", 0.95, input_cost=1e-07, output_cost=2e-07, matcher="structured")
        c2 = _candidate("qwen3-72b", 0.85, input_cost=3e-07, output_cost=6e-07, matcher="structured")
        result = aggregate([c1, c2], "qwen3-32b")
        assert result is not None
        assert result.strategy == "aggregated"

        total_weight = 0.95 + 0.85
        expected_input = (1e-07 * 0.95 + 3e-07 * 0.85) / total_weight
        expected_output = (2e-07 * 0.95 + 6e-07 * 0.85) / total_weight
        assert abs(result.input_cost_per_token - expected_input) < 1e-15
        assert abs(result.output_cost_per_token - expected_output) < 1e-15

    def test_dedup_by_key_keeps_highest_score(self):
        c1 = _candidate("deepseek-chat", 0.80, matcher="fuzzy")
        c2 = _candidate("deepseek-chat", 1.0, matcher="exact")
        result = aggregate([c1, c2], "deepseek-chat")
        assert result is not None
        assert result.matched_keys == ("deepseek-chat",)
        assert result.aggregate_score == 1.0
        assert result.strategy == "exact"

    def test_mixed_above_and_below_threshold(self):
        c1 = _candidate("qwen3-32b", 0.95, matcher="structured")
        c2 = _candidate("qwen3-72b", 0.50, matcher="structured")
        result = aggregate([c1, c2], "qwen3-32b", threshold=0.80)
        assert result is not None
        assert result.matched_keys == ("qwen3-32b",)
        assert result.strategy == "structured"

    def test_aggregate_score_is_minimum_of_filtered(self):
        c1 = _candidate("qwen3-32b", 0.95)
        c2 = _candidate("qwen3-72b", 0.85)
        result = aggregate([c1, c2], "qwen3-32b")
        assert result is not None
        assert result.aggregate_score == 0.85

    def test_zero_total_weight_returns_none(self):
        c1 = _candidate("qwen3-32b", 0.0)
        c2 = _candidate("qwen3-72b", 0.0)
        result = aggregate([c1, c2], "qwen3-32b", threshold=0.0)
        assert result is None
