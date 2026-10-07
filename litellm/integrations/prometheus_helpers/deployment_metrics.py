"""Per-deployment telemetry for the OpenRouter ``/endpoints`` view.

One deployment's live and windowed load, read from Prometheus and keyed by
``model_id`` (the deployment identity every relevant metric carries as a label).
Feeds the ``PublicEndpoint`` telemetry fields (``latency_last_30m``,
``throughput_last_30m``, ``uptime_last_*``) plus this gateway's live-concurrency
extension.

Semantics match OpenRouter's contract and the source decision in
``docs/openrouter/MAPPING-usage-metrics.md`` §8g/8h:

- latency is **time to first token** (streaming only; the TTFT histogram is
  observed for streamed requests), reported in milliseconds.
- throughput is **per-request generation speed** (tokens/second), the inverse of
  the ``litellm_deployment_latency_per_output_token`` histogram.
- uptime is ``success / (success + failure) * 100`` over the window.

The reader runs its queries concurrently and caches the whole ``model_id`` map
for a short TTL, so a burst of ``/endpoints`` calls costs at most one query set
per TTL rather than one per request.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from litellm._logging import verbose_logger
from litellm.integrations.prometheus_helpers.prometheus_api import (
    PROMETHEUS_URL,
    query_prometheus_instant,
)

_WINDOW: Final = "30m"
_CACHE_TTL_SECONDS: Final = 15.0

_QUANTILES: Final[tuple[tuple[str, float], ...]] = (("p50", 0.50), ("p75", 0.75), ("p90", 0.90), ("p99", 0.99))

_UPTIME_WINDOWS: Final[tuple[tuple[str, str], ...]] = (
    ("uptime_last_5m", "5m"),
    ("uptime_last_30m", "30m"),
    ("uptime_last_1d", "1d"),
)


@dataclass(frozen=True, slots=True)
class Percentiles:
    """A four-point percentile distribution."""

    p50: float
    p75: float
    p90: float
    p99: float


@dataclass(frozen=True, slots=True)
class PerDeploymentMetrics:
    """Everything the ``/endpoints`` telemetry fields need for one deployment."""

    live_concurrency: int | None = None
    ttft_latency_ms: Percentiles | None = None
    throughput_tokens_per_sec: Percentiles | None = None
    requests_last_30m: float | None = None
    uptime_last_5m: float | None = None
    uptime_last_30m: float | None = None
    uptime_last_1d: float | None = None


def _series_by_model_id(raw: list[dict]) -> dict[str, float]:
    """Flatten an instant-query result into ``model_id -> value``, dropping NaN."""
    out: dict[str, float] = {}
    for entry in raw:
        model_id = entry.get("metric", {}).get("model_id", "")
        if not model_id:
            continue
        value = float(entry.get("value", [0, "0"])[1])
        if math.isnan(value):
            continue
        out[model_id] = value
    return out


def _histogram_quantile_query(metric: str, quantile: float, window: str) -> str:
    return f"histogram_quantile({quantile}, sum by (le, model_id) (rate({metric}_bucket[{window}])))"


def _success_query(window: str) -> str:
    return f"sum by (model_id) (increase(litellm_deployment_success_responses_total[{window}]))"


def _failure_query(window: str) -> str:
    return f"sum by (model_id) (increase(litellm_deployment_failure_responses_total[{window}]))"


def _uptime_percent(success: float, failure: float) -> float:
    total = success + failure
    return success / total * 100.0 if total > 0 else 0.0


async def _query(query: str) -> dict[str, float]:
    try:
        return _series_by_model_id(await query_prometheus_instant(query))
    except Exception as e:  # noqa: BLE001  # one failed metric must not blank the whole view
        verbose_logger.debug("per-deployment metrics query failed (%s): %s", query, e)
        return {}


def _percentiles_from(values: dict[str, float], transform: Callable[[float], float]) -> Percentiles | None:
    """Build a ``Percentiles`` when all four quantiles resolved for one model_id.

    ``values`` maps quantile name -> raw value; ``transform`` converts a raw
    value into the reported unit (seconds to ms, or seconds-per-token to
    tokens/second). Returns ``None`` when any quantile is missing, so a partial
    distribution is reported as absent rather than fabricated.
    """
    if len(values) < len(_QUANTILES):
        return None
    return Percentiles(
        p50=transform(values["p50"]),
        p75=transform(values["p75"]),
        p90=transform(values["p90"]),
        p99=transform(values["p99"]),
    )


class DeploymentMetricsReader:
    """Reads and caches per-deployment telemetry from Prometheus."""

    def __init__(self, cache_ttl_seconds: float = _CACHE_TTL_SECONDS) -> None:
        self._cache_ttl_seconds = cache_ttl_seconds
        # ``None`` means "never fetched"; an empty dict is a valid snapshot (no
        # deployments had data), so it must still count as a cache hit.
        self._cache: dict[str, PerDeploymentMetrics] | None = None
        self._cache_at: float = 0.0
        self._lock = asyncio.Lock()

    async def read(self, model_ids: list[str]) -> dict[str, PerDeploymentMetrics]:
        """Return telemetry for the requested deployments.

        Reads the whole ``model_id`` map (cached) and returns the requested
        subset; an unknown ``model_id`` is simply absent.
        """
        if PROMETHEUS_URL is None or not model_ids:
            return {}
        snapshot = await self._snapshot()
        return {model_id: snapshot[model_id] for model_id in model_ids if model_id in snapshot}

    async def _snapshot(self) -> dict[str, PerDeploymentMetrics]:
        now = time.monotonic()
        if self._cache is not None and now - self._cache_at < self._cache_ttl_seconds:
            return self._cache
        async with self._lock:
            now = time.monotonic()
            if self._cache is not None and now - self._cache_at < self._cache_ttl_seconds:
                return self._cache
            fetched = await self._fetch()
            self._cache = fetched
            self._cache_at = time.monotonic()
            return fetched

    async def _fetch(self) -> dict[str, PerDeploymentMetrics]:
        ttft_queries = [_histogram_quantile_query("litellm_llm_api_time_to_first_token_metric", q, _WINDOW) for _, q in _QUANTILES]
        throughput_queries = [_histogram_quantile_query("litellm_deployment_latency_per_output_token", q, _WINDOW) for _, q in _QUANTILES]
        # Success and failure are read separately rather than as one ratio: a
        # deployment with zero failures has no failure series at all, and a
        # PromQL vector division against a missing operand yields an empty
        # vector (uptime would vanish for the healthiest deployments).
        success_queries = [_success_query(window) for _, window in _UPTIME_WINDOWS]
        failure_queries = [_failure_query(window) for _, window in _UPTIME_WINDOWS]
        other_queries = [
            "sum by (model_id) (litellm_deployment_in_progress_requests)",
            f"sum by (model_id) (increase(litellm_deployment_total_requests_total[{_WINDOW}]))",
        ]

        all_queries = [*ttft_queries, *throughput_queries, *success_queries, *failure_queries, *other_queries]
        results = await asyncio.gather(*(_query(q) for q in all_queries))

        n_ttft = len(ttft_queries)
        n_throughput = len(throughput_queries)
        n_windows = len(_UPTIME_WINDOWS)
        ttft_results = results[:n_ttft]
        throughput_results = results[n_ttft : n_ttft + n_throughput]
        success_results = results[n_ttft + n_throughput : n_ttft + n_throughput + n_windows]
        failure_results = results[n_ttft + n_throughput + n_windows : n_ttft + n_throughput + 2 * n_windows]
        concurrency_by_id = results[n_ttft + n_throughput + 2 * n_windows]
        requests_by_id = results[n_ttft + n_throughput + 2 * n_windows + 1]

        model_ids = {
            *concurrency_by_id,
            *requests_by_id,
            *(mid for r in ttft_results for mid in r),
            *(mid for r in throughput_results for mid in r),
            *(mid for r in success_results for mid in r),
            *(mid for r in failure_results for mid in r),
        }

        def _quantiles_for(results_for_metric: list[dict[str, float]], model_id: str) -> dict[str, float]:
            return {
                name: r[model_id] for (name, _), r in zip(_QUANTILES, results_for_metric, strict=True) if model_id in r
            }

        def _uptime_for(window_index: int, model_id: str) -> float | None:
            success = success_results[window_index].get(model_id)
            if success is None:
                return None
            failure = failure_results[window_index].get(model_id, 0.0)
            return _uptime_percent(success, failure)

        def _build(model_id: str) -> PerDeploymentMetrics:
            ttft = _quantiles_for(ttft_results, model_id)
            throughput = _quantiles_for(throughput_results, model_id)
            return PerDeploymentMetrics(
                live_concurrency=int(concurrency_by_id[model_id]) if model_id in concurrency_by_id else None,
                ttft_latency_ms=_percentiles_from(ttft, lambda seconds: seconds * 1000.0),
                throughput_tokens_per_sec=_percentiles_from(
                    throughput, lambda seconds_per_token: 1.0 / seconds_per_token if seconds_per_token > 0 else 0.0
                ),
                requests_last_30m=requests_by_id.get(model_id),
                uptime_last_5m=_uptime_for(0, model_id),
                uptime_last_30m=_uptime_for(1, model_id),
                uptime_last_1d=_uptime_for(2, model_id),
            )

        return {model_id: _build(model_id) for model_id in model_ids}
