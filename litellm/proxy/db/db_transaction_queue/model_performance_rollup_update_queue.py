"""
In-memory buffer for model performance rollup transactions.

Each request contributes one ``ModelPerformanceRollupTransaction`` (per minute
bucket). These are buffered in memory, flushed to Redis periodically, then
batch-upserted into ``LiteLLM_ModelPerformanceRollup`` by a dedicated drain job.

The buffer aggregates multiple per-bucket transactions into a single one via the
(commutative, associative) rollup merge monoid before handing them to Redis, so
the number of items written to Redis and ultimately upserted into Postgres stays
small even under high request rates.
"""

import asyncio
from copy import deepcopy
from math import floor, log, pow
from typing import Dict, List, Optional

from litellm._logging import verbose_proxy_logger
from litellm.constants import LITELLM_ASYNCIO_QUEUE_MAXSIZE
from litellm.proxy._types import ModelPerformanceRollupTransaction
from litellm.proxy.db.db_transaction_queue.base_update_queue import (
    BaseUpdateQueue,
    service_logger_obj,
)
from litellm.types.services import ServiceTypes

# Number of log-bucketed bins for the TTFT histogram. Fixed and small so the
# merge operation is a cheap element-wise array add. Covers roughly 0.1s to
# 1000s of TTFT with 32 bins.
TTFT_HISTOGRAM_NUM_BINS = 32
TTFT_HISTOGRAM_MIN = 0.1
TTFT_HISTOGRAM_MAX = 1000.0


def build_ttft_histogram(
    ttft_seconds: Optional[float],
    edges: List[float],
) -> List[int]:
    """Build a count array with a single value placed in its bin (or all zeros)."""
    counts = [0] * len(edges)
    if ttft_seconds is None:
        return counts
    counts[_bin_for_value(ttft_seconds, edges)] = 1
    return counts


def ttft_histogram_edges() -> List[float]:
    """Return the fixed log-spaced bin edges for the TTFT histogram.

    Bin ``i`` covers ``[edges[i], edges[i+1])`` for ``i < len(edges) - 1``, and
    the final edge is +inf (values above the max fall into the last bin).
    The edges array has ``TTFT_HISTOGRAM_NUM_BINS + 1`` entries.
    """
    log_min = floor(log(TTFT_HISTOGRAM_MIN))
    log_max = log(TTFT_HISTOGRAM_MAX)
    edges = [
        pow(10, log_min + (log_max - log_min) * i / TTFT_HISTOGRAM_NUM_BINS) for i in range(TTFT_HISTOGRAM_NUM_BINS)
    ]
    edges.append(float("inf"))
    return edges


def _bin_for_value(value: float, edges: List[float]) -> int:
    """Return the histogram bin index for a TTFT value (in seconds)."""
    # Linear scan over ~33 edges is cheap and avoids a binary-search dependency.
    for i in range(len(edges) - 1):
        if value < edges[i + 1]:
            return i
    return len(edges) - 2


def add_histogram_counts(target: List[int], incoming: List[int]) -> List[int]:
    """Element-wise add of two histogram count arrays."""
    return [t + i for t, i in zip(target, incoming)]


def histogram_percentile(percentile: float, edges: List[float], counts: List[int]) -> Optional[float]:
    """Reconstruct a percentile from a log-bucketed histogram.

    Returns the mid-point of the bin where the cumulative count crosses the
    requested percentile, or ``None`` if the histogram has no observations.
    """
    total = sum(counts)
    if total <= 0:
        return None
    target = percentile * total
    cumulative = 0
    for i, count in enumerate(counts):
        cumulative += count
        if cumulative >= target:
            # Bin i covers [edges[i], edges[i+1]). The final bin's upper edge is
            # infinite, so there is no finite midpoint; fall back to the last
            # finite edge as the representative point.
            if edges[i + 1] == float("inf"):
                return float(edges[i])
            mid = (edges[i] + edges[i + 1]) / 2.0
            return float(mid)
    return None


class ModelPerformanceRollupUpdateQueue(BaseUpdateQueue):
    """In-memory buffer for model performance rollup transactions."""

    def __init__(self):
        super().__init__()
        self.update_queue: asyncio.Queue[Dict[str, ModelPerformanceRollupTransaction]] = asyncio.Queue(
            maxsize=LITELLM_ASYNCIO_QUEUE_MAXSIZE
        )

    async def add_update(self, update: Dict[str, ModelPerformanceRollupTransaction]):
        """Enqueue an update."""
        verbose_proxy_logger.debug("Adding model performance rollup update to queue: %s", update)
        await self.update_queue.put(update)
        if self.update_queue.qsize() >= self.MAX_SIZE_IN_MEMORY_QUEUE:
            verbose_proxy_logger.warning(
                "Model performance rollup queue is full. Aggregating all entries in queue to concatenate entries."
            )
            await self.aggregate_queue_updates()

    async def aggregate_queue_updates(self):
        """Combine all updates in the queue into a single update to bound queue size."""
        updates: List[
            Dict[str, ModelPerformanceRollupTransaction]
        ] = await self.flush_all_updates_from_in_memory_queue()
        aggregated_updates = self.get_aggregated_rollup_transactions(updates)
        await self.update_queue.put(aggregated_updates)

    async def flush_and_get_aggregated_rollup_transactions(
        self,
    ) -> Dict[str, ModelPerformanceRollupTransaction]:
        """Flush all in-memory updates and return them aggregated by bucket key."""
        updates = await self.flush_all_updates_from_in_memory_queue()
        if len(updates) > 0:
            verbose_proxy_logger.info(
                "Model performance rollup - flushed %d items from in-memory queue",
                len(updates),
            )
        return self.get_aggregated_rollup_transactions(updates)

    @staticmethod
    def get_aggregated_rollup_transactions(
        updates: List[Dict[str, ModelPerformanceRollupTransaction]],
    ) -> Dict[str, ModelPerformanceRollupTransaction]:
        """Merge per-bucket transactions into a single transaction per bucket key.

        Bucket key is ``model_group::bucket_start``.
        """
        aggregated: Dict[str, ModelPerformanceRollupTransaction] = {}
        for _update in updates:
            for _key, payload in _update.items():
                if _key in aggregated:
                    aggregated[_key] = ModelPerformanceRollupUpdateQueue.merge_transactions(aggregated[_key], payload)
                else:
                    aggregated[_key] = deepcopy(payload)
        return aggregated

    @staticmethod
    def merge_transactions(
        a: ModelPerformanceRollupTransaction,
        b: ModelPerformanceRollupTransaction,
    ) -> ModelPerformanceRollupTransaction:
        """Merge two per-bucket rollup transactions into one (monoid combine)."""
        combined = deepcopy(a)
        combined["request_count"] += b["request_count"]
        combined["completion_tokens"] += b["completion_tokens"]
        combined["throughput_tokens_sum"] += b["throughput_tokens_sum"]
        combined["ttft_seconds_sum"] += b["ttft_seconds_sum"]
        combined["ttft_seconds_sum_sq"] += b["ttft_seconds_sum_sq"]

        # Element-wise histogram add. Edge arrays are identical for the same
        # bucket (fixed log edges), so we can zip them safely.
        combined["ttft_histogram_counts"] = add_histogram_counts(
            combined["ttft_histogram_counts"], b["ttft_histogram_counts"]
        )

        # min/max combine
        a_min = combined["ttft_seconds_min"]
        b_min = b["ttft_seconds_min"]
        combined["ttft_seconds_min"] = (
            min(a_min, b_min) if a_min is not None and b_min is not None else (a_min if a_min is not None else b_min)
        )
        a_max = combined["ttft_seconds_max"]
        b_max = b["ttft_seconds_max"]
        combined["ttft_seconds_max"] = (
            max(a_max, b_max) if a_max is not None and b_max is not None else (a_max if a_max is not None else b_max)
        )

        combined["starts"] += b["starts"]
        combined["ends"] += b["ends"]
        return combined

    async def _emit_new_item_added_to_queue_event(
        self,
        queue_size: Optional[int] = None,
    ):
        asyncio.create_task(
            service_logger_obj.async_service_success_hook(
                service=ServiceTypes.IN_MEMORY_MODEL_PERFORMANCE_ROLLUP_UPDATE_QUEUE,
                duration=0,
                call_type="_emit_new_item_added_to_queue_event",
                event_metadata={
                    "gauge_labels": ServiceTypes.IN_MEMORY_MODEL_PERFORMANCE_ROLLUP_UPDATE_QUEUE,
                    "gauge_value": queue_size,
                },
            )
        )
