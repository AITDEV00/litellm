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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

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


def normalize_api_base_for_gauge(api_base: str) -> str:
    if not api_base:
        return ""
    stripped = api_base.rstrip("/")
    for suffix in _API_BASE_ENDPOINT_SUFFIXES:
        if stripped.endswith(suffix):
            return stripped[: -len(suffix)].rstrip("/")
    return stripped


class DeploymentInFlightLedger:
    """Per-process authoritative counter behind ``litellm_deployment_in_progress_requests``.

    The gauge is a raw ``livesum`` gauge incremented at pre-call and
    decremented at success/failure. Historically it was driven with bare
    ``.inc()``/``.dec()`` calls whose label tuples were derived from different
    sources in the inc vs dec paths. When those sources disagreed (e.g. one
    path resolving ``api_base`` and the other not, or a provider prefix
    mismatch) the inc and dec hit *different* label series that never cancel,
    so the gauge leaked and a deployment that stopped receiving traffic still
    reported a phantom 1 forever.

    This ledger fixes that at the root. Because ``model_id`` is the stable
    deployment identity that is present and consistent in both the pre-call and
    post-call payloads, it keys the count by ``model_id`` alone. The label set
    used to *display* the series is reconciled to a single canonical set per
    model: any label variant previously emitted for the same ``model_id`` is
    set back to 0, so a stray series can never linger as a phantom nonzero.

    The ledger is per-process (it owns the process-local gauge), which matches
    the existing behaviour whether the proxy runs a single worker or several
    granian workers without ``PROMETHEUS_MULTIPROC_DIR``.
    """

    __slots__ = ("_counts", "_canonical_labels", "_emitted_series", "_lock")

    def __init__(self) -> None:
        # model_id -> in-flight count (>= 0)
        self._counts: dict[str, int] = {}
        # model_id -> canonical label tuple currently emitted
        self._canonical_labels: dict[str, tuple[str, str, str, str]] = {}
        # model_id -> set of label tuples previously emitted for it
        self._emitted_series: dict[str, set[tuple[str, str, str, str]]] = {}
        self._lock = threading.Lock()

    def reconcile(
        self,
        model_id: str,
        litellm_model_name: str,
        api_base: str,
        api_provider: str,
        delta: int,
        emit: Callable[[tuple[str, str, str, str], int], None],
    ) -> None:
        """Apply ``delta`` to ``model_id`` and emit the reconciled gauge value.

        ``emit(labels_tuple, value)`` is a callback that sets the gauge for a
        given label tuple to ``value``. It is called with the lock held so the
        reconcile-and-emit sequence is atomic with respect to concurrent inc/dec.
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
            current_count = max(0, self._counts.get(model_id, 0) + delta)
            self._counts[model_id] = current_count

            emitted_series = self._emitted_series.setdefault(model_id, set())
            emitted_series.add(previous_labels)
            for stale_labels in tuple(emitted_series):
                if stale_labels == previous_labels:
                    continue
                emit(stale_labels, 0)
                emitted_series.discard(stale_labels)

            emit(previous_labels, current_count)
            self._canonical_labels[model_id] = previous_labels


class DeploymentInFlightMetricsMixin:
    """In-flight gauge inc/dec logic for the Prometheus logger (OICM-custom)."""

    # The gauge is registered by the concrete logger's ``__init__``.
    litellm_deployment_in_progress_requests: Any
    _deployment_in_flight_ledger: DeploymentInFlightLedger

    def _reconcile_deployment_in_flight(
        self,
        model_id: str,
        litellm_model_name: str,
        api_base: str,
        api_provider: str,
        delta: int,
    ) -> None:
        """Route a +1/-1 through the in-flight ledger and emit the reconciled gauge.

        The gauge is set (not incremented/decremented) from the authoritative
        per-``model_id`` count so that any label drift between the inc and dec
        paths self-heals: a label variant previously emitted for the same
        ``model_id`` is reset to 0, and the count is clamped to >= 0.
        """
        if not model_id:
            return

        # Lazy import to avoid a circular dependency: prometheus_label_factory
        # lives in the (upstream) prometheus module that imports this mixin.
        from litellm.integrations.prometheus import prometheus_label_factory  # noqa: PLC0415
        from litellm.types.integrations.prometheus import UserAPIKeyLabelValues

        def _emit(labels_tuple: tuple[str, str, str, str], value: int) -> None:
            name, mid, base, provider = labels_tuple
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

        self._deployment_in_flight_ledger.reconcile(
            model_id=model_id,
            litellm_model_name=litellm_model_name,
            api_base=api_base,
            api_provider=api_provider,
            delta=delta,
            emit=_emit,
        )

    def _inc_deployment_in_progress(self, model: str, kwargs: dict[str, Any]) -> None:
        try:
            standard_logging_payload: Optional[StandardLoggingPayload] = kwargs.get("standard_logging_object")
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
            else:
                model_id = standard_logging_payload.get("model_id", "") or ""
                if not model_id:
                    return
                litellm_model_name = standard_logging_payload.get("model", "") or model
                api_provider = standard_logging_payload.get("custom_llm_provider", "") or _litellm_params.get(
                    "custom_llm_provider", ""
                )
                api_base = _litellm_params.get("api_base", "") or standard_logging_payload.get("api_base", "")
            self._reconcile_deployment_in_flight(
                model_id=model_id,
                litellm_model_name=litellm_model_name,
                api_base=api_base,
                api_provider=api_provider,
                delta=1,
            )
        except Exception as e:  # noqa: BLE001
            verbose_logger.debug("Prometheus: _inc_deployment_in_progress error: {}".format(str(e)))

    async def async_pre_call_deployment_hook(
        self,
        kwargs: dict[str, Any],
        call_type: Optional[CallTypes],
    ) -> Optional[dict]:
        model = kwargs.get("model", "")
        self._inc_deployment_in_progress(model, kwargs)
        return None