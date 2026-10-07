"""In-flight deployment gauge for Prometheus (OICM-custom).

Co-located slice for the ``litellm_deployment_in_progress_requests`` gauge.
The entire gauge (ledger, api_base normalization, label extraction, inc/dec
hooks) is OICM-custom and does not exist upstream. Keeping it in its own
module means upstream's ``prometheus.py`` stays conflict-free on merges and
only wires the gauge registration and the two dec call-sites.

The mixin ``DeploymentInFlightMetricsMixin`` provides the methods that
``PrometheusLogger`` inherits. It relies on the following attributes existing
on the concrete logger (set by ``PrometheusLogger.__init__``):

- ``self.litellm_deployment_in_progress_requests`` -- the gauge
- ``self._deployment_in_flight_ledger`` -- the authoritative per-model ledger
- ``self.get_labels_for_metric`` -- resolves the metric's supported label set
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import litellm
from litellm._logging import verbose_logger
from litellm.litellm_core_utils.core_helpers import (
    get_litellm_metadata_from_kwargs,
    get_metadata_variable_name_from_kwargs,
)

if TYPE_CHECKING:
    from litellm.types.utils import CallTypes, StandardLoggingPayload


_API_BASE_ENDPOINT_SUFFIXES: tuple[str, ...] = (
    "/chat/completions",
    "/completions",
    "/embeddings",
    "/responses",
    "/rerank",
    "/transcriptions",
    "/translations",
    "/images/generations",
    "/audio/speech",
)

# A request older than this cannot still be in flight: the router enforces
# timeout/stream_timeout (600s/300s on prod) on top of the upstream call, so a
# registry entry older than the floor is by definition an inc whose dec never
# fired (crashed logging path, aborted client, task cancelled mid-hook).
# Evicting at the floor bounds the worst-case phantom-concurrency window
# instead of letting it freeze forever, which is what prod showed (644/690
# "in flight" on a deployment with zero traffic for days).
_IN_FLIGHT_TTL_SECONDS: float = 1800.0

# Each granian worker owns its own registry, so a scrape served by worker A can
# only evict worker A's stale entries; the other workers' stale entries keep
# being summed by MultiProcessCollector until a scrape happens to land on each.
# This background sweep runs inside every worker that has admitted a request, so
# every worker self-heals on its own clock instead of depending on scrape
# routing. Interval is well under the TTL, so worst-case staleness stays ~TTL.
_IN_FLIGHT_SWEEP_INTERVAL_SECONDS: float = 60.0


class _SweeperState:
    __slots__ = ("started",)

    def __init__(self) -> None:
        self.started = False


_sweeper_state = _SweeperState()
_sweeper_lock = threading.Lock()


def evict_all_prometheus_loggers() -> None:
    """Run TTL eviction on every live Prometheus logger in this process."""
    try:
        import litellm
        from litellm.integrations.prometheus import PrometheusLogger

        for prometheus_logger in litellm.logging_callback_manager.get_custom_loggers_for_type(
            callback_type=PrometheusLogger
        ):
            prometheus_logger.evict_stale_deployment_in_flight()
    except Exception:  # noqa: BLE001  # housekeeping must never surface to a caller
        verbose_logger.debug("Prometheus: in-flight eviction skipped (prometheus logger unavailable)")


def _sweeper_loop() -> None:
    """Periodically evict TTL-expired in-flight entries for this worker."""
    while True:
        time.sleep(_IN_FLIGHT_SWEEP_INTERVAL_SECONDS)
        evict_all_prometheus_loggers()


def ensure_in_flight_sweeper_started() -> None:
    """Start the per-worker TTL sweeper once per process.

    Triggered from the admit path so it only ever starts inside a worker that
    actually has in-flight state to sweep (never the pre-fork master, whose
    threads do not survive ``fork``).
    """
    with _sweeper_lock:
        if _sweeper_state.started:
            return
        _sweeper_state.started = True
    threading.Thread(target=_sweeper_loop, name="litellm-in-flight-sweeper", daemon=True).start()


def normalize_api_base_for_gauge(api_base: str) -> str:
    if not api_base:
        return ""
    stripped = api_base.rstrip("/")
    for suffix in _API_BASE_ENDPOINT_SUFFIXES:
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)].rstrip("/")
    return stripped


class DeploymentInFlightLedger:
    """Authoritative per-request registry behind ``litellm_deployment_in_progress_requests``.

    The gauge is a raw ``livesum`` gauge whose value must equal "how many
    requests are between the pre-call hook and their terminal logging event"
    per deployment. Driving it with bare ``.inc()``/``.dec()`` deltas leaked in
    every shape where an inc has no matching dec: router retries re-enter the
    wrapped function and inc again while the terminal logging events fire once
    per logical request, failures whose logging path raises before the metrics
    block never dec at all, and client aborts can end the task without either
    terminal event (prod: 644/690 frozen "in-flight" on a deployment idle for
    days, and a busy deployment showing 418 while DB peak concurrency was 9).

    This registry makes correctness structural instead of pairing-dependent:

    - Entries are keyed by ``model_id`` + ``litellm_call_id`` (stable across
      retries and fallback hops, set once per logical request in the ``@client``
      wrapper). Inc is an idempotent add, dec an idempotent remove, so
      N retries add one entry and any single terminal event removes it.
    - The gauge value is derived (``len(entries)``), set via the emit callback,
      so it can never drift from the registry.
    - Stale entries (a dec that never fired) are evicted after a TTL strictly
      above the deployment's configured request timeouts, which bounds the
      phantom-concurrency window instead of freezing it forever. Eviction runs
      on every mutation, from the scrape path, and from a per-worker background
      sweeper, so an idle deployment self-heals without a dec ever firing.

    The registry is per-process, matching the livesum aggregation model: each
    granian worker owns its in-flight set and ``PROMETHEUS_MULTIPROC_DIR``
    sums the per-worker values into the per-pod total.
    """

    __slots__ = ("_canonical_labels", "_entries", "_lock")

    def __init__(self) -> None:
        # model_id -> litellm_call_id -> admission monotonic timestamp
        self._entries: dict[str, dict[str, float]] = {}
        # model_id -> canonical label tuple currently emitted
        self._canonical_labels: dict[str, tuple[str, str, str, str]] = {}
        self._lock = threading.Lock()

    def admit(
        self,
        model_id: str,
        call_id: str,
        started_at: float,
        litellm_model_name: str,
        api_base: str,
        api_provider: str,
        emit: Callable[[tuple[str, str, str, str], int], None],
    ) -> None:
        """Idempotently register an in-flight request and emit the new gauge value.

        Labels are assigned once, here: the dec paths never re-derive them,
        so an inc/dec label mismatch (the original leak) is impossible by
        construction. A repeated admit for the same ``call_id`` (router retry,
        fallback hop) keeps the existing entry and its original start time.
        """
        with self._lock:
            previous_labels = self._canonical_labels.get(model_id)
            if previous_labels is None:
                previous_labels = (
                    litellm_model_name,
                    model_id,
                    normalize_api_base_for_gauge(api_base),
                    api_provider,
                )
                self._canonical_labels[model_id] = previous_labels
            entries = self._entries.setdefault(model_id, {})
            entries.setdefault(call_id, started_at)
            self._evict_expired_locked(model_id, entries)
            emit(previous_labels, len(entries))

    def release(
        self,
        model_id: str,
        call_id: str,
        emit: Callable[[tuple[str, str, str, str], int], None],
    ) -> None:
        """Idempotently remove an in-flight request and emit the new gauge value.

        Releasing a ``call_id`` that was never admitted (or already released)
        is a no-op: the count is the size of the live entry set, so a stray
        dec can neither drive it negative nor cancel an unrelated request.
        """
        with self._lock:
            labels = self._canonical_labels.get(model_id)
            if labels is None:
                return
            entries = self._entries.get(model_id)
            if entries is None:
                return
            self._evict_expired_locked(model_id, entries)
            entries.pop(call_id, None)
            if not entries:
                self._entries.pop(model_id, None)
            emit(labels, len(entries))

    def evict_expired(self, emit: Callable[[tuple[str, str, str, str], int], None]) -> None:
        """Drop entries older than the TTL across all deployments and re-emit.

        Called from the scrape path and the per-worker sweeper, so a deployment
        whose last request lost its dec still returns to 0 once the request is
        provably over.
        """
        with self._lock:
            for model_id in tuple(self._entries):
                entries = self._entries.get(model_id)
                if entries is None:
                    continue
                if self._evict_expired_locked(model_id, entries):
                    labels = self._canonical_labels.get(model_id)
                    if labels is not None:
                        emit(labels, len(entries))

    def _evict_expired_locked(self, model_id: str, entries: dict[str, float]) -> bool:
        """Evict expired entries in place; True when anything was dropped."""
        cutoff = time.monotonic() - _IN_FLIGHT_TTL_SECONDS
        expired = [call_id for call_id, started_at in entries.items() if started_at < cutoff]
        for call_id in expired:
            del entries[call_id]
        if expired:
            verbose_logger.debug(
                "Prometheus: evicted %d stale in-flight entries for model_id=%s (no dec fired within TTL)",
                len(expired),
                model_id,
            )
        return bool(expired)


class DeploymentInFlightMetricsMixin:
    """In-flight gauge admit/release logic for the Prometheus logger (OICM-custom)."""

    # The gauge is registered by the concrete logger's ``__init__``.
    litellm_deployment_in_progress_requests: Any
    _deployment_in_flight_ledger: DeploymentInFlightLedger

    def _emit_deployment_in_flight(self, labels_tuple: tuple[str, str, str, str], value: int) -> None:
        # Called with the ledger lock held: the count-and-emit sequence must be
        # atomic so the emitted value can never interleave with a concurrent
        # admit/release and land stale.
        name, mid, base, provider = labels_tuple
        # Lazy import to avoid a circular dependency: prometheus_label_factory
        # lives in the (upstream) prometheus module that imports this mixin.
        from litellm.integrations.prometheus import prometheus_label_factory  # noqa: PLC0415
        from litellm.types.integrations.prometheus import UserAPIKeyLabelValues

        _in_progress_labels = prometheus_label_factory(
            supported_enum_labels=self.get_labels_for_metric("litellm_deployment_in_progress_requests"),
            enum_values=UserAPIKeyLabelValues(
                litellm_model_name=name,
                model_id=mid,
                api_base=base,
                api_provider=provider,
            ),
        )
        self.litellm_deployment_in_progress_requests.labels(**_in_progress_labels).set(value)

    def _admit_deployment_in_flight(
        self,
        model_id: str,
        litellm_model_name: str,
        api_base: str,
        api_provider: str,
        call_id: str,
    ) -> None:
        """Register one in-flight request in the authoritative registry.

        The gauge value is derived from the registry size, so the inc/dec
        label drift and retry double-count classes of leak cannot occur.
        """
        if not model_id:
            return

        # Start the per-worker sweeper here (first admit in this process) rather
        # than at import/boot: threads started before granian forks do not
        # survive into the workers, so this guarantees the sweeper lives in each
        # worker that actually holds in-flight state.
        ensure_in_flight_sweeper_started()
        self._deployment_in_flight_ledger.admit(
            model_id=model_id,
            call_id=call_id,
            started_at=time.monotonic(),
            litellm_model_name=litellm_model_name,
            api_base=api_base,
            api_provider=api_provider,
            emit=self._emit_deployment_in_flight,
        )

    def _release_deployment_in_flight(self, model_id: str, call_id: str) -> None:
        """Remove one in-flight request from the authoritative registry.

        Idempotent per ``call_id``: the terminal logging events that dec here
        fire once per logical request while the pre-call hook fires once per
        attempt, so every extra release must be a no-op rather than a
        negative-count push.
        """
        if not model_id:
            return

        self._deployment_in_flight_ledger.release(
            model_id=model_id,
            call_id=call_id,
            emit=self._emit_deployment_in_flight,
        )

    def evict_stale_deployment_in_flight(self) -> None:
        """Drop registry entries whose dec never fired.

        Invoked from the scrape path and from the per-worker background
        sweeper. A request older than the TTL cannot still be in flight (the
        router enforces timeout/stream_timeout below it), so the entry is by
        definition leaked. This is the backstop for terminal events that never
        run at all: failure paths that raise before the metrics block, and
        client aborts that end the task without a terminal hook.
        """
        self._deployment_in_flight_ledger.evict_expired(emit=self._emit_deployment_in_flight)

    def _inc_deployment_in_progress(self, model: str, kwargs: dict[str, Any]) -> None:
        try:
            standard_logging_payload: StandardLoggingPayload | None = kwargs.get("standard_logging_object")
            _litellm_params = kwargs.get("litellm_params", {}) or {}
            if standard_logging_payload is None:
                _metadata = get_litellm_metadata_from_kwargs(kwargs)
                if not _metadata:
                    _meta_key = get_metadata_variable_name_from_kwargs(kwargs)
                    _metadata = kwargs.get(_meta_key, {}) or {}
                model_info = _metadata.get("model_info", {})
                model_id = model_info.get("id", "") if isinstance(model_info, dict) else ""
                if not model_id:
                    return
                litellm_model_name = model
                api_provider = (
                    _litellm_params.get("custom_llm_provider", "")
                    or _metadata.get("custom_llm_provider", "")
                    or kwargs.get("custom_llm_provider", "")
                )
                if not api_provider and litellm_model_name:
                    try:
                        _, _parsed_provider, _, _ = litellm.get_llm_provider(
                            model=litellm_model_name,
                            custom_llm_provider=None,
                        )
                        api_provider = _parsed_provider
                    except Exception:  # noqa: BLE001
                        pass
                api_base = (
                    _litellm_params.get("api_base", "") or _metadata.get("api_base", "") or kwargs.get("api_base", "")
                )
                call_id = str(kwargs.get("litellm_call_id") or _litellm_params.get("litellm_call_id") or "")
            else:
                model_id = standard_logging_payload.get("model_id", "") or ""
                if not model_id:
                    return
                litellm_model_name = standard_logging_payload.get("model", "") or model
                api_provider = standard_logging_payload.get("custom_llm_provider", "") or _litellm_params.get(
                    "custom_llm_provider", ""
                )
                api_base = _litellm_params.get("api_base", "") or standard_logging_payload.get("api_base", "")
                call_id = str(standard_logging_payload.get("litellm_call_id") or kwargs.get("litellm_call_id") or "")
            if not call_id:
                # Without a stable per-request key an admit cannot be deduped
                # across retries, so it is safer to skip than to double-count.
                verbose_logger.debug(
                    "Prometheus: _inc_deployment_in_progress skipped, no litellm_call_id for model_id=%s", model_id
                )
                return
            self._admit_deployment_in_flight(
                model_id=model_id,
                litellm_model_name=litellm_model_name,
                api_base=api_base,
                api_provider=api_provider,
                call_id=call_id,
            )
        except Exception as e:  # noqa: BLE001
            verbose_logger.debug(f"Prometheus: _inc_deployment_in_progress error: {e!s}")

    async def async_pre_call_deployment_hook(
        self,
        kwargs: dict[str, Any],
        call_type: CallTypes | None,
    ) -> dict | None:
        model = kwargs.get("model", "")
        self._inc_deployment_in_progress(model, kwargs)
        return None
