"""Periodic memory monitor for the LiteLLM proxy.

Samples process RSS, GC state, thread count and Prometheus series count on a
schedule, logging one structured line per sample so growth is visible in
container logs (and therefore Loki). When RSS grows beyond a threshold
between samples, opens a bounded tracemalloc window and logs the top growing
allocation sites — the "which function is causing it" answer.

Overhead design (see docs/performance/retention-flags-and-oom-LOGIC-MAP-v2.md):
- Tier 1 (always): one /proc read + registry walk per sample. ~0% cost.
- Tier 2 (duty-cycled): tracemalloc depth 1 for a bounded window, only when
  growth is detected, always stopped in a finally block. Never left running.

The monitor retains exactly one previous sample and one previous snapshot —
each cycle replaces, never appends, so the monitor itself cannot leak.
"""

import asyncio
import gc
import os
import threading
import time
from typing import Final

from litellm._logging import verbose_proxy_logger

_MONITOR_LOG_PREFIX: Final = "mem_monitor"

# Tier 2 thresholds. RSS growth between samples beyond this opens one
# tracemalloc attribution window.
_GROWTH_ALERT_MB: Final = 200.0
_ATTRIBUTION_WINDOW_SECONDS: Final = 300.0
_TRACEMALLOC_DEPTH: Final = 1
_TOP_GROWING_SITES: Final = 10

_DEFAULT_SAMPLE_INTERVAL_SECONDS: Final = 60


def _current_rss_mb() -> float | None:
    """Current process RSS in MB, read from /proc (NOT ru_maxrss, which is a
    high-water mark and cannot show creep)."""
    try:
        with open("/proc/self/statm", encoding="ascii") as statm:
            resident_pages: Final = int(statm.read().split()[1])
    except Exception:  # noqa: BLE001 - a sampling problem must never fail the proxy
        return None
    return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)


def _prometheus_series_count() -> int | None:
    """Total number of live label-series across all Prometheus collectors.

    Growing series count = metric-cardinality growth, the top suspect for
    slow RSS creep in a long-lived gateway.
    """
    try:
        from prometheus_client import REGISTRY

        # REGISTRY exposes no series-count API, so walk the private
        # collector map: each collector maps to its metric names, and each
        # name expands to one series per label combination (sampled via
        # collect() so the count reflects live label children).
        total: Final = 0
        for collector in REGISTRY._collector_to_names:
            for metric_family in collector.collect():
                for _sample in metric_family.samples:
                    total += 1
    except Exception:  # noqa: BLE001 - a sampling problem must never fail the proxy
        return None
    return total


def _gc_summary() -> str:
    """Per-generation object counts from the garbage collector."""
    counts: Final = [str(gen.get("collections", 0)) for gen in gc.get_stats()]
    objects: Final = len(gc.get_objects())
    return f"gc_collections={','.join(counts)} gc_objects={objects}"


def _log_sample(extra: str = "") -> None:
    """Emit one structured mem_monitor line with all Tier-1 gauges."""
    rss: Final = _current_rss_mb()
    series: Final = _prometheus_series_count()
    verbose_proxy_logger.info(
        "%s rss_mb=%s peak_rss_mb=%s prom_series=%s threads=%d %s %s",
        _MONITOR_LOG_PREFIX,
        f"{rss:.1f}" if rss is not None else "unknown",
        f"{_peak_rss_mb():.1f}" if _peak_rss_mb() is not None else "unknown",
        series,
        threading.active_count(),
        _gc_summary(),
        extra,
    )


def _peak_rss_mb() -> float | None:
    """High-water RSS in MB (ru_maxrss) — context for the current value."""
    try:
        import resource

        ru_maxrss: Final = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except Exception:  # noqa: BLE001 - a sampling problem must never fail the proxy
        return None
    return float(ru_maxrss) / 1024


class MemoryMonitor:
    """Scheduler-driven sampler: Tier 1 every interval, Tier 2 on growth."""

    def __init__(
        self,
        sample_interval_seconds: int = _DEFAULT_SAMPLE_INTERVAL_SECONDS,
        attribution_window_seconds: float = _ATTRIBUTION_WINDOW_SECONDS,
    ) -> None:
        """Retains only the previous sample/snapshot: each cycle replaces,
        never appends, so the monitor itself cannot leak."""
        self.sample_interval_seconds: Final = sample_interval_seconds
        self.attribution_window_seconds: Final = attribution_window_seconds
        self._last_rss_mb: float | None = None
        self._last_sample_monotonic: float | None = None
        self._attribution_running: bool = False

    async def sample(self) -> None:
        """One scheduled sample. Cheap; safe to run concurrently with traffic."""
        rss: Final = _current_rss_mb()
        if rss is None:
            verbose_proxy_logger.warning("%s could not read RSS", _MONITOR_LOG_PREFIX)
            return

        extra_parts: list[str] = []
        previous: Final = self._last_rss_mb
        if previous is not None:
            delta: Final = rss - previous
            extra_parts.append(f"delta_mb={delta:+.1f}")
            if delta > _GROWTH_ALERT_MB and not self._attribution_running:
                extra_parts.append("growth_detected=true arming_tracemalloc=true")
        self._last_rss_mb = rss
        self._last_sample_monotonic = time.monotonic()

        _log_sample(" ".join(extra_parts))

        if previous is not None and (rss - previous) > _GROWTH_ALERT_MB:
            await self._run_attribution_window()

    async def _run_attribution_window(self) -> None:
        """Tier 2: tracemalloc for a bounded window, log top growing sites, stop.

        Always stops the tracer — even on error — so tracing memory is never
        left behind (tracemalloc itself is the classic source of a monitor
        leak if left running).
        """
        if self._attribution_running:
            return
        self._attribution_running = True
        try:
            import tracemalloc

            verbose_proxy_logger.info(
                "%s tracemalloc attribution window starting (%.0fs, depth=%d)",
                _MONITOR_LOG_PREFIX,
                self.attribution_window_seconds,
                _TRACEMALLOC_DEPTH,
            )
            tracemalloc.start(_TRACEMALLOC_DEPTH)
            before: Final = tracemalloc.take_snapshot()
            await asyncio.sleep(self.attribution_window_seconds)
            after: Final = tracemalloc.take_snapshot()
            diff: Final = after.compare_to(before, "lineno")
            verbose_proxy_logger.info(
                "%s tracemalloc top growing allocation sites over %.0fs:",
                _MONITOR_LOG_PREFIX,
                self.attribution_window_seconds,
            )
            for stat in diff[:_TOP_GROWING_SITES]:
                frame: Final = stat.traceback[0]
                verbose_proxy_logger.info(
                    "%s +%.2f MB (%d blocks) %s:%d (%s)",
                    _MONITOR_LOG_PREFIX,
                    stat.size_diff / (1024 * 1024),
                    stat.count_diff,
                    frame.filename,
                    frame.lineno,
                    stat.traceback.format()[-1].strip()[:120],
                )
            traced: Final = tracemalloc.get_traced_memory()
            verbose_proxy_logger.info(
                "%s tracemalloc traced current=%.1fMB peak=%.1fMB",
                _MONITOR_LOG_PREFIX,
                traced[0] / (1024 * 1024),
                traced[1] / (1024 * 1024),
            )
        except Exception as e:  # noqa: BLE001 - the monitor must never crash the proxy
            verbose_proxy_logger.warning("%s attribution window failed: %s", _MONITOR_LOG_PREFIX, e)
        finally:
            try:
                import tracemalloc

                tracemalloc.stop()
            except Exception as stop_err:  # noqa: BLE001 - releasing the tracer must never crash the proxy
                verbose_proxy_logger.warning(
                    "%s tracemalloc.stop() failed: %s", _MONITOR_LOG_PREFIX, stop_err
                )
            self._attribution_running = False


def get_sample_interval_seconds() -> int:
    """Env-configured sample interval; 0 disables the monitor."""
    raw: Final = os.getenv("LITELLM_MEMORY_MONITOR_INTERVAL")
    if raw is None:
        return _DEFAULT_SAMPLE_INTERVAL_SECONDS
    try:
        return int(raw)
    except ValueError:
        verbose_proxy_logger.warning(
            "Invalid LITELLM_MEMORY_MONITOR_INTERVAL %r; using default %ds",
            raw,
            _DEFAULT_SAMPLE_INTERVAL_SECONDS,
        )
        return _DEFAULT_SAMPLE_INTERVAL_SECONDS
