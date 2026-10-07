"""Per-deployment telemetry for the OpenRouter ``/endpoints`` view.

One deployment's live and windowed load, read from Prometheus and keyed by
``model_id`` (the deployment identity every relevant metric carries as a label).
Feeds the ``PublicEndpoint`` telemetry fields (``latency_last_30m``,
``throughput_last_30m``, ``uptime_last_*``) plus this gateway's live-concurrency
extension.

This lives in the slice's enrichment layer rather than the shared Prometheus
helpers because the decisions here are all OpenRouter-contract decisions: the
units, the window, and the uptime formula. Only the generic Prometheus client
(``query_prometheus_instant``) is shared.

- latency is time to first token (streaming only), in milliseconds.
- throughput is per-request generation speed (tokens/second), the inverse of the
  ``litellm_deployment_latency_per_output_token`` histogram.
- uptime is ``success / (success + failure) * 100`` over the window.

The reader runs its queries concurrently and caches the whole ``model_id`` map
for a short TTL, so a burst of ``/endpoints`` calls costs one query set per TTL
rather than one per request.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol

from litellm._logging import verbose_logger
from litellm.integrations.prometheus_helpers.prometheus_api import (
    PROMETHEUS_URL,
    query_prometheus_instant,
)

_WINDOW: Final = "30m"
_CACHE_TTL_SECONDS: Final = 15.0
_TTFT_METRIC: Final = "litellm_llm_api_time_to_first_token_metric"
_THROUGHPUT_METRIC: Final = "litellm_deployment_latency_per_output_token"

_QUANTILES: Final[tuple[tuple[str, float], ...]] = (
    ("p50", 0.50),
    ("p75", 0.75),
    ("p90", 0.90),
    ("p99", 0.99),
)

_UPTIME_WINDOWS: Final[tuple[str, ...]] = ("5m", "30m", "1d")

_EMPTY: Final[Mapping[str, float]] = MappingProxyType({})


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


class DeploymentTelemetryReader(Protocol):
    """The read surface the models service depends on.

    Depending on this port rather than the concrete Prometheus reader lets a
    test inject a fake reader instead of patching the reader's module internals.
    """

    async def read(self, model_ids: Sequence[str]) -> Mapping[str, PerDeploymentMetrics]: ...


@dataclass(frozen=True, slots=True)
class _QuerySpec:
    """One PromQL query and the key its result is stored under."""

    key: str
    promql: str


def _histogram_quantile_query(metric: str, quantile: float, window: str) -> str:
    return f"histogram_quantile({quantile}, sum by (le, model_id) (rate({metric}_bucket[{window}])))"


def _success_query(window: str) -> str:
    return f"sum by (model_id) (increase(litellm_deployment_success_responses_total[{window}]))"


def _failure_query(window: str) -> str:
    return f"sum by (model_id) (increase(litellm_deployment_failure_responses_total[{window}]))"


def _query_specs() -> tuple[_QuerySpec, ...]:
    """Every query a snapshot needs, each keyed so results never depend on order."""
    return (
        *(_QuerySpec(f"ttft_{label}", _histogram_quantile_query(_TTFT_METRIC, q, _WINDOW)) for label, q in _QUANTILES),
        *(
            _QuerySpec(f"throughput_{label}", _histogram_quantile_query(_THROUGHPUT_METRIC, q, _WINDOW))
            for label, q in _QUANTILES
        ),
        *(_QuerySpec(f"success_{window}", _success_query(window)) for window in _UPTIME_WINDOWS),
        *(_QuerySpec(f"failure_{window}", _failure_query(window)) for window in _UPTIME_WINDOWS),
        _QuerySpec("concurrency", "sum by (model_id) (litellm_deployment_in_progress_requests)"),
        _QuerySpec("requests", f"sum by (model_id) (increase(litellm_deployment_total_requests_total[{_WINDOW}]))"),
    )


def _uptime_percent(success: float, failure: float) -> float:
    total: Final = success + failure
    return success / total * 100.0 if total > 0 else 0.0


def _model_id_and_value(entry: Mapping[str, object]) -> tuple[str, float] | None:
    """Read one instant-query sample, or ``None`` when it has no usable value."""
    metric: Final = entry.get("metric")
    model_id: Final = metric.get("model_id") if isinstance(metric, Mapping) else None
    if not isinstance(model_id, str) or not model_id:
        return None
    value: Final = entry.get("value")
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) < 2:
        return None
    parsed: Final = float(value[1])  # pyright: ignore[reportArgumentType]  # Prometheus renders the value as a numeric string
    return None if math.isnan(parsed) else (model_id, parsed)


def _series_by_model_id(raw: Sequence[Mapping[str, object]]) -> Mapping[str, float]:
    """Flatten a single-value instant-query result into ``model_id -> value``, dropping NaN."""
    pairs: Final = tuple(pair for entry in raw if (pair := _model_id_and_value(entry)) is not None)
    return MappingProxyType(dict(pairs))


def _flatten_by_model_id(series: Mapping[str, Mapping[str, float]]) -> Mapping[str, Mapping[str, float]]:
    """Invert ``query key -> (model_id -> value)`` into ``model_id -> (query key -> value)``."""
    model_ids: Final = frozenset(mid for group in series.values() for mid in group)
    return MappingProxyType(
        {model_id: MappingProxyType({key: group[model_id] for key, group in series.items() if model_id in group}) for model_id in model_ids}
    )


async def _query(promql: str) -> Mapping[str, float]:
    try:
        return _series_by_model_id(await query_prometheus_instant(promql))
    except Exception as e:  # noqa: BLE001  # one failed metric must not blank the whole view
        verbose_logger.debug("per-deployment metrics query failed (%s): %s", promql, e)
        return _EMPTY


def _percentiles_from(values: Mapping[str, float], transform: Callable[[float], float]) -> Percentiles | None:
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


def _seconds_to_ms(seconds: float) -> float:
    return seconds * 1000.0


def _tokens_per_second(seconds_per_token: float) -> float:
    return 1.0 / seconds_per_token if seconds_per_token > 0 else 0.0


def _quantile_values(cells: Mapping[str, float], prefix: str) -> Mapping[str, float]:
    return MappingProxyType(
        {label: cells[f"{prefix}_{label}"] for label, _ in _QUANTILES if f"{prefix}_{label}" in cells}
    )


def _uptime_for(window: str, cells: Mapping[str, float]) -> float | None:
    """Uptime for one window, or ``None`` when the deployment had no success in it.

    Success and failure are read separately rather than as one ratio: a
    deployment with zero failures has no failure series at all, and a PromQL
    vector division against a missing operand yields an empty vector (uptime
    would vanish for the healthiest deployments). So a missing failure term
    defaults to zero, while a missing success term means no traffic.
    """
    success: Final = cells.get(f"success_{window}")
    if success is None:
        return None
    return _uptime_percent(success, cells.get(f"failure_{window}", 0.0))


def _build_one(cells: Mapping[str, float]) -> PerDeploymentMetrics:
    concurrency: Final = cells.get("concurrency")
    return PerDeploymentMetrics(
        live_concurrency=int(concurrency) if concurrency is not None else None,
        ttft_latency_ms=_percentiles_from(_quantile_values(cells, "ttft"), _seconds_to_ms),
        throughput_tokens_per_sec=_percentiles_from(_quantile_values(cells, "throughput"), _tokens_per_second),
        requests_last_30m=cells.get("requests"),
        uptime_last_5m=_uptime_for(_UPTIME_WINDOWS[0], cells),
        uptime_last_30m=_uptime_for(_UPTIME_WINDOWS[1], cells),
        uptime_last_1d=_uptime_for(_UPTIME_WINDOWS[2], cells),
    )


def _snapshot_from(flat: Mapping[str, Mapping[str, float]]) -> Mapping[str, PerDeploymentMetrics]:
    return MappingProxyType({model_id: _build_one(cells) for model_id, cells in flat.items()})


class PrometheusDeploymentTelemetryReader:
    """Reads and caches per-deployment telemetry from Prometheus."""

    def __init__(self, cache_ttl_seconds: float = _CACHE_TTL_SECONDS) -> None:
        self._cache_ttl_seconds = cache_ttl_seconds
        # ``None`` means "never fetched"; an empty mapping is a valid snapshot
        # (no deployments had data), so it must still count as a cache hit.
        self._cache: Mapping[str, PerDeploymentMetrics] | None = None
        self._cache_at: float = 0.0
        self._lock = asyncio.Lock()
        self._query_specs: Final = _query_specs()

    async def read(self, model_ids: Sequence[str]) -> Mapping[str, PerDeploymentMetrics]:
        """Return telemetry for the requested deployments.

        Reads the whole ``model_id`` map (cached) and returns the requested
        subset; an unknown ``model_id`` is simply absent.
        """
        if PROMETHEUS_URL is None or not model_ids:
            return MappingProxyType({})
        snapshot: Final = await self._snapshot()
        return MappingProxyType({model_id: snapshot[model_id] for model_id in model_ids if model_id in snapshot})

    async def _snapshot(self) -> Mapping[str, PerDeploymentMetrics]:
        now: Final = time.monotonic()
        if self._cache is not None and now - self._cache_at < self._cache_ttl_seconds:
            return self._cache
        async with self._lock:
            now_locked: Final = time.monotonic()
            if self._cache is not None and now_locked - self._cache_at < self._cache_ttl_seconds:
                return self._cache
            fetched: Final = await self._fetch()
            self._cache = fetched
            self._cache_at = time.monotonic()
            return fetched

    async def _fetch(self) -> Mapping[str, PerDeploymentMetrics]:
        results: Final = await asyncio.gather(*(_query(spec.promql) for spec in self._query_specs))
        series: Final[Mapping[str, Mapping[str, float]]] = MappingProxyType(
            {spec.key: result for spec, result in zip(self._query_specs, results, strict=True)}
        )
        return _snapshot_from(_flatten_by_model_id(series))
