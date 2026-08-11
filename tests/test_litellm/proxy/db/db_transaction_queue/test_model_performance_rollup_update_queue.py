import os
import sys

import pytest

sys.path.insert(
    0, os.path.abspath("../../..")
)  # Adds the parent directory to the system path

from litellm.proxy._types import ModelPerformanceRollupTransaction
from litellm.proxy.db.db_transaction_queue.model_performance_rollup_update_queue import (
    ModelPerformanceRollupUpdateQueue,
    add_histogram_counts,
    build_ttft_histogram,
    histogram_percentile,
    ttft_histogram_edges,
)

EDGES = ttft_histogram_edges()


def _tx(
    model_group: str = "gpt-4o",
    bucket_start: str = "2026-08-11T00:00:00",
    request_count: int = 1,
    completion_tokens: int = 0,
    throughput_tokens_sum: float = 0.0,
    ttft_seconds_sum: float = 0.0,
    ttft_seconds_sum_sq: float = 0.0,
    ttft_seconds_min: float | None = None,
    ttft_seconds_max: float | None = None,
    ttft_histogram_counts: list[int] | None = None,
    starts: int = 0,
    ends: int = 0,
) -> ModelPerformanceRollupTransaction:
    return ModelPerformanceRollupTransaction(
        model_group=model_group,
        bucket_start=bucket_start,
        request_count=request_count,
        completion_tokens=completion_tokens,
        throughput_tokens_sum=throughput_tokens_sum,
        ttft_seconds_sum=ttft_seconds_sum,
        ttft_seconds_sum_sq=ttft_seconds_sum_sq,
        ttft_seconds_min=ttft_seconds_min,
        ttft_seconds_max=ttft_seconds_max,
        ttft_histogram_edges=EDGES,
        ttft_histogram_counts=ttft_histogram_counts
        if ttft_histogram_counts is not None
        else [0] * len(EDGES),
        starts=starts,
        ends=ends,
    )


class TestTtftHistogramEdges:
    def test_edges_are_monotonic_and_infinite_final_edge(self):
        assert len(EDGES) == 33
        assert EDGES[-1] == float("inf")
        for i in range(len(EDGES) - 1):
            assert EDGES[i] < EDGES[i + 1]


class TestBuildTtftHistogram:
    def test_none_ttft_returns_all_zeros(self):
        counts = build_ttft_histogram(None, EDGES)
        assert counts == [0] * len(EDGES)

    def test_value_places_in_correct_bin(self):
        counts = build_ttft_histogram(5.0, EDGES)
        assert sum(counts) == 1
        # bin index must be one that actually covers 5.0
        idx = counts.index(1)
        assert EDGES[idx] <= 5.0 < EDGES[idx + 1]


class TestAddHistogramCounts:
    def test_element_wise_add(self):
        a = [1, 2, 3]
        b = [4, 5, 6]
        assert add_histogram_counts(a, b) == [5, 7, 9]

    def test_preserves_length(self):
        assert len(add_histogram_counts([0] * 33, [0] * 33)) == 33


class TestHistogramPercentile:
    def test_empty_histogram_returns_none(self):
        assert histogram_percentile(0.5, EDGES, [0] * len(EDGES)) is None

    def test_single_value_returns_its_bin_midpoint(self):
        counts = build_ttft_histogram(1.0, EDGES)
        p50 = histogram_percentile(0.5, EDGES, counts)
        assert p50 is not None
        assert 0.0 < p50 < 10.0

    def test_all_values_in_high_bin(self):
        # A value larger than the last finite edge should land in the final
        # (infinite) bin, and the percentile should clamp to that finite edge.
        counts = build_ttft_histogram(10 ** 7, EDGES)
        p50 = histogram_percentile(0.5, EDGES, counts)
        assert p50 is not None
        assert p50 == EDGES[-2]


class TestMergeTransactions:
    def test_merges_scalar_fields(self):
        a = _tx(request_count=2, completion_tokens=10, throughput_tokens_sum=5.0)
        b = _tx(request_count=3, completion_tokens=4, throughput_tokens_sum=1.5)
        merged = ModelPerformanceRollupUpdateQueue.merge_transactions(a, b)
        assert merged["request_count"] == 5
        assert merged["completion_tokens"] == 14
        assert merged["throughput_tokens_sum"] == 6.5

    def test_merges_ttft_sum_and_sumsq(self):
        a = _tx(ttft_seconds_sum=2.0, ttft_seconds_sum_sq=4.0)
        b = _tx(ttft_seconds_sum=3.0, ttft_seconds_sum_sq=9.0)
        merged = ModelPerformanceRollupUpdateQueue.merge_transactions(a, b)
        assert merged["ttft_seconds_sum"] == 5.0
        assert merged["ttft_seconds_sum_sq"] == 13.0

    def test_merges_min_and_max(self):
        a = _tx(ttft_seconds_min=1.0, ttft_seconds_max=4.0)
        b = _tx(ttft_seconds_min=0.5, ttft_seconds_max=10.0)
        merged = ModelPerformanceRollupUpdateQueue.merge_transactions(a, b)
        assert merged["ttft_seconds_min"] == 0.5
        assert merged["ttft_seconds_max"] == 10.0

    def test_merges_min_max_with_none(self):
        a = _tx(ttft_seconds_min=2.0, ttft_seconds_max=None)
        b = _tx(ttft_seconds_min=None, ttft_seconds_max=5.0)
        merged = ModelPerformanceRollupUpdateQueue.merge_transactions(a, b)
        assert merged["ttft_seconds_min"] == 2.0
        assert merged["ttft_seconds_max"] == 5.0

    def test_merges_starts_and_ends(self):
        a = _tx(starts=1, ends=1)
        b = _tx(starts=3, ends=2)
        merged = ModelPerformanceRollupUpdateQueue.merge_transactions(a, b)
        assert merged["starts"] == 4
        assert merged["ends"] == 3

    def test_merges_histogram_counts(self):
        a = _tx(ttft_histogram_counts=build_ttft_histogram(1.0, EDGES))
        b = _tx(ttft_histogram_counts=build_ttft_histogram(1.0, EDGES))
        merged = ModelPerformanceRollupUpdateQueue.merge_transactions(a, b)
        assert merged["ttft_histogram_counts"] == [
            2 if c else 0 for c in build_ttft_histogram(1.0, EDGES)
        ]

    def test_does_not_mutate_inputs(self):
        a = _tx(request_count=1)
        b = _tx(request_count=2)
        _ = ModelPerformanceRollupUpdateQueue.merge_transactions(a, b)
        assert a["request_count"] == 1
        assert b["request_count"] == 2


class TestGetAggregatedRollupTransactions:
    def test_merges_same_bucket_across_dicts(self):
        key = "gpt-4o::2026-08-11T00:00:00"
        updates = [
            {key: _tx(request_count=1, completion_tokens=5)},
            {key: _tx(request_count=2, completion_tokens=7)},
        ]
        aggregated = ModelPerformanceRollupUpdateQueue.get_aggregated_rollup_transactions(updates)
        assert len(aggregated) == 1
        assert aggregated[key]["request_count"] == 3
        assert aggregated[key]["completion_tokens"] == 12

    def test_keeps_distinct_buckets_separate(self):
        key1 = "gpt-4o::2026-08-11T00:00:00"
        key2 = "gpt-4o::2026-08-11T00:01:00"
        updates = [
            {key1: _tx(request_count=1)},
            {key2: _tx(request_count=1)},
        ]
        aggregated = ModelPerformanceRollupUpdateQueue.get_aggregated_rollup_transactions(updates)
        assert set(aggregated.keys()) == {key1, key2}


class TestQueueFlush:
    @pytest.mark.asyncio
    async def test_empty_queue_flush_returns_empty(self):
        queue = ModelPerformanceRollupUpdateQueue()
        result = await queue.flush_and_get_aggregated_rollup_transactions()
        assert result == {}

    @pytest.mark.asyncio
    async def test_flush_aggregates_updates(self):
        queue = ModelPerformanceRollupUpdateQueue()
        key = "gpt-4o::2026-08-11T00:00:00"
        await queue.add_update({key: _tx(request_count=1, completion_tokens=3)})
        await queue.add_update({key: _tx(request_count=2, completion_tokens=4)})
        result = await queue.flush_and_get_aggregated_rollup_transactions()
        assert result[key]["request_count"] == 3
        assert result[key]["completion_tokens"] == 7