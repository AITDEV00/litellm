"""
Tests for the litellm_deployment_in_progress_requests gauge inc/dec contract.

The gauge must return to 0 after every LLM call completes (success or failure).
A missed decrement causes the gauge to climb forever.

We test the inc/dec contract at three levels:
1. The gauge itself: inc then dec returns to 0; two incs leaves 2
2. async_pre_call_deployment_hook: incs the gauge when model_id present; noop when absent
3. set_llm_deployment_failure_metrics / set_llm_deployment_success_metrics: decs
   the gauge after the call completes

Plus the three measured prod leak modes (2026-10-07 audit,
docs/openrouter/MAPPING-usage-metrics.md):
4. Router retries re-enter the pre-call hook but the terminal logging events
   fire once per logical request: an admit keyed by litellm_call_id must be
   idempotent and a repeated release a no-op.
5. A terminal event that never fires (crashed logging path, client abort)
   must be healed by TTL eviction at scrape time, not frozen forever.
6. Failure logging with a partially-populated standard_logging_object must
   not raise before the metrics are recorded.
"""

import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import CollectorRegistry, Gauge, Histogram, generate_latest

import litellm
from litellm.integrations.prometheus import PrometheusLogger
from litellm.integrations.prometheus_helpers.deployment_in_flight import (
    DeploymentInFlightLedger,
    normalize_api_base_for_gauge,
)
from litellm.types.integrations.prometheus import (
    PrometheusMetricLabels,
    UserAPIKeyLabelValues,
)


@pytest.fixture
def isolated_registry():
    reg = CollectorRegistry()
    yield reg


@pytest.fixture(autouse=True)
def _no_real_sweeper_thread():
    """Keep the per-worker sweeper from spawning a real (60s-sleeping) thread in tests.

    Pre-marks the process-global sweeper as already started so the admit path's
    ``ensure_in_flight_sweeper_started`` returns immediately. Tests that need to
    observe the start behavior set ``started = False`` and patch ``threading.Thread``.
    """
    from litellm.integrations.prometheus_helpers import deployment_in_flight as slice_mod

    original = slice_mod._sweeper_state.started
    slice_mod._sweeper_state.started = True
    try:
        yield
    finally:
        slice_mod._sweeper_state.started = original


@pytest.fixture
def logger(isolated_registry):
    """Create a PrometheusLogger with only the in-progress gauge registered."""
    with patch(
        "litellm.integrations.prometheus.PrometheusLogger.__init__",
        return_value=None,
    ):
        pl = PrometheusLogger()
        pl.litellm_deployment_in_progress_requests = Gauge(
            "litellm_deployment_in_progress_requests",
            "Number of LLM API calls currently in progress per deployment",
            labelnames=["litellm_model_name", "model_id", "api_base", "api_provider"],
            multiprocess_mode="livesum",
            registry=isolated_registry,
        )
        pl.litellm_deployment_total_requests = Gauge(
            "litellm_deployment_total_requests",
            "Total requests",
            labelnames=PrometheusMetricLabels.get_labels("litellm_deployment_total_requests"),
            registry=isolated_registry,
        )
        pl.litellm_deployment_failure_responses = Gauge(
            "litellm_deployment_failure_responses",
            "Failure responses",
            labelnames=PrometheusMetricLabels.get_labels("litellm_deployment_failure_responses"),
            registry=isolated_registry,
        )
        pl.litellm_deployment_success_responses = Gauge(
            "litellm_deployment_success_responses",
            "Success responses",
            labelnames=PrometheusMetricLabels.get_labels("litellm_deployment_success_responses"),
            registry=isolated_registry,
        )
        pl.litellm_deployment_state = Gauge(
            "litellm_deployment_state",
            "Deployment state",
            labelnames=PrometheusMetricLabels.get_labels("litellm_deployment_state"),
            registry=isolated_registry,
        )
        pl.litellm_deployment_latency_per_output_token = Histogram(
            "litellm_deployment_latency_per_output_token",
            "Latency per output token",
            labelnames=PrometheusMetricLabels.get_labels("litellm_deployment_latency_per_output_token"),
            registry=isolated_registry,
        )
        pl.litellm_overhead_latency_metric = Histogram(
            "litellm_overhead_latency_metric",
            "Overhead latency",
            labelnames=PrometheusMetricLabels.get_labels("litellm_overhead_latency_metric"),
            registry=isolated_registry,
        )
        pl.litellm_overhead_with_guardrails_latency_metric = Histogram(
            "litellm_overhead_with_guardrails_latency_metric",
            "Overhead with guardrails latency",
            labelnames=PrometheusMetricLabels.get_labels("litellm_overhead_with_guardrails_latency_metric"),
            registry=isolated_registry,
        )
        pl._bounded_prometheus_series_tracker = MagicMock()
        pl._cached_metric_labels: dict = {}
        pl.label_filters: dict = {}
        pl.exclude_metrics: frozenset = frozenset()
        pl.exclude_labels: frozenset = frozenset()
        pl._deployment_in_flight_ledger = DeploymentInFlightLedger()
        return pl


def _gauge_value(registry, model_id="abc-123"):
    output = generate_latest(registry).decode()
    for line in output.splitlines():
        if "litellm_deployment_in_progress_requests" in line and f'model_id="{model_id}"' in line:
            return float(line.split()[-1])
    return 0.0


def test_gauge_inc_then_dec_returns_to_zero(logger, isolated_registry):
    g = logger.litellm_deployment_in_progress_requests
    labels = g.labels(
        litellm_model_name="Qwen3.6-35B",
        model_id="abc-123",
        api_base="http://vllm:8000",
        api_provider="hosted_vllm",
    )
    labels.inc()
    assert _gauge_value(isolated_registry) == 1.0

    labels.dec()
    assert _gauge_value(isolated_registry) == 0.0


def test_gauge_two_inc_without_dec_shows_two(logger, isolated_registry):
    g = logger.litellm_deployment_in_progress_requests
    labels = g.labels(
        litellm_model_name="Qwen3.6-35B",
        model_id="abc-123",
        api_base="http://vllm:8000",
        api_provider="hosted_vllm",
    )
    labels.inc()
    labels.inc()
    assert _gauge_value(isolated_registry) == 2.0


@pytest.mark.asyncio
async def test_pre_call_incs_gauge_when_model_id_present(logger, isolated_registry):
    kwargs = {
        "model": "Qwen3.6-35B",
        "messages": [],
        "litellm_call_id": "call-simple-inc",
        "standard_logging_object": {
            "model_id": "abc-123",
            "api_base": "http://vllm:8000",
            "model": "Qwen3.6-35B",
            "custom_llm_provider": "hosted_vllm",
            "litellm_call_id": "call-simple-inc",
        },
    }
    await logger.async_pre_call_deployment_hook(kwargs=kwargs, call_type=None)
    assert _gauge_value(isolated_registry) == 1.0


@pytest.mark.asyncio
async def test_pre_call_noop_when_model_id_missing(logger, isolated_registry):
    kwargs = {
        "model": "Qwen3.6-35B",
        "messages": [],
        "standard_logging_object": {
            "model_id": "",
            "api_base": "",
            "model": "Qwen3.6-35B",
            "custom_llm_provider": "hosted_vllm",
        },
    }
    await logger.async_pre_call_deployment_hook(kwargs=kwargs, call_type=None)
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_pre_call_noop_when_standard_logging_missing(logger, isolated_registry):
    kwargs = {
        "model": "Qwen3.6-35B",
        "messages": [],
        "litellm_params": {
            "metadata": {"model_info": {}},
        },
    }
    await logger.async_pre_call_deployment_hook(kwargs=kwargs, call_type=None)
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_deployment_hook_incs_gauge_with_only_litellm_params(logger, isolated_registry):
    """Production scenario: async_pre_call_deployment_hook fires after the
    router picks a deployment but before standard_logging_object is populated.
    The hook must still inc the gauge using kwargs.metadata.model_info
    (the router puts model_info in top-level metadata, not litellm_params).
    The @client wrapper seeds litellm_call_id on kwargs before this hook runs."""
    kwargs = {
        "model": "Qwen3.6-35B",
        "messages": [],
        "litellm_call_id": "call-router-path",
        "metadata": {
            "model_info": {"id": "abc-123"},
            "api_base": "http://vllm:8000/v1/chat/completions",
            "custom_llm_provider": "hosted_vllm",
        },
    }
    await logger.async_pre_call_deployment_hook(kwargs=kwargs, call_type=None)
    assert _gauge_value(isolated_registry) == 1.0


@pytest.mark.asyncio
async def test_failure_metrics_decs_gauge(logger, isolated_registry):
    await logger.async_pre_call_deployment_hook(
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_params": {"api_base": "http://vllm:8000", "custom_llm_provider": "hosted_vllm"},
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "litellm_call_id": "call-a",
            },
        },
        call_type=None,
    )
    assert _gauge_value(isolated_registry) == 1.0

    logger.set_llm_deployment_failure_metrics(
        {
            "model": "Qwen3.6-35B",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "model_group": "Qwen3.6-35B",
                "litellm_call_id": "call-a",
            },
            "litellm_params": {"custom_llm_provider": "hosted_vllm", "api_base": "http://vllm:8000"},
            "exception": Exception("timeout"),
        }
    )
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_failure_metrics_no_dec_when_model_id_missing(logger, isolated_registry):
    await logger.async_pre_call_deployment_hook(
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_call_id": "call-no-dec",
            "litellm_params": {"api_base": "http://vllm:8000", "custom_llm_provider": "hosted_vllm"},
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "litellm_call_id": "call-no-dec",
            },
        },
        call_type=None,
    )
    assert _gauge_value(isolated_registry) == 1.0

    logger.set_llm_deployment_failure_metrics(
        {
            "model": "Qwen3.6-35B",
            "standard_logging_object": {
                "model_id": None,
                "api_base": None,
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
            },
            "litellm_params": {},
            "exception": Exception("timeout"),
        }
    )
    assert _gauge_value(isolated_registry) == 1.0


@pytest.mark.asyncio
async def test_success_metrics_decs_gauge(logger, isolated_registry):
    await logger.async_pre_call_deployment_hook(
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_params": {"api_base": "http://vllm:8080", "custom_llm_provider": "hosted_vllm"},
            "litellm_call_id": "call-b",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8080",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "litellm_call_id": "call-b",
            },
        },
        call_type=None,
    )
    assert _gauge_value(isolated_registry) == 1.0

    start = datetime(2026, 1, 1, 0, 0, 0)
    end = datetime(2026, 1, 1, 0, 0, 5)
    enum_values = UserAPIKeyLabelValues(
        litellm_model_name="Qwen3.6-35B",
        model_id="abc-123",
        api_base="http://vllm:8080",
        api_provider="hosted_vllm",
    )
    logger.set_llm_deployment_success_metrics(
        request_kwargs={
            "model": "Qwen3.6-35B",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8080",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "model_group": "Qwen3.6-35B",
                "hidden_params": {"additional_headers": {}, "litellm_overhead_time_ms": 0},
                "metadata": {},
                "completion_tokens": 10,
                "litellm_call_id": "call-b",
            },
            "litellm_params": {
                "custom_llm_provider": "hosted_vllm",
                "api_base": "http://vllm:8080",
                "metadata": {"model_info": {"id": "abc-123"}},
            },
        },
        start_time=start,
        end_time=end,
        enum_values=enum_values,
        output_tokens=10.0,
    )
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_failure_metrics_decs_gauge_when_litellm_params_missing_provider(logger, isolated_registry):
    """Regression: dec must use the same label source as inc.

    The inc path reads api_provider from standard_logging_object. If the dec
    path reads it from litellm_params instead (which may be missing the key),
    the dec creates a different label series and the gauge leaks.

    This test reproduces the original bug: litellm_params has no
    custom_llm_provider, but standard_logging_object does.
    """
    await logger.async_pre_call_deployment_hook(
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_params": {"api_base": "http://vllm:8000"},
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "litellm_call_id": "call-a",
            },
        },
        call_type=None,
    )
    assert _gauge_value(isolated_registry) == 1.0

    logger.set_llm_deployment_failure_metrics(
        {
            "model": "Qwen3.6-35B",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "model_group": "Qwen3.6-35B",
                "litellm_call_id": "call-a",
            },
            "litellm_params": {"api_base": "http://vllm:8000"},
            "exception": Exception("timeout"),
        }
    )
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_success_metrics_decs_gauge_when_litellm_params_missing_provider(logger, isolated_registry):
    """Regression: same as failure test but for the success path."""
    await logger.async_pre_call_deployment_hook(
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_params": {"api_base": "http://vllm:8080"},
            "litellm_call_id": "call-d",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8080",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "litellm_call_id": "call-d",
            },
        },
        call_type=None,
    )
    assert _gauge_value(isolated_registry) == 1.0

    start = datetime(2026, 1, 1, 0, 0, 0)
    end = datetime(2026, 1, 1, 0, 0, 5)
    enum_values = UserAPIKeyLabelValues(
        litellm_model_name="Qwen3.6-35B",
        model_id="abc-123",
        api_base="http://vllm:8080",
        api_provider="hosted_vllm",
    )
    logger.set_llm_deployment_success_metrics(
        request_kwargs={
            "model": "Qwen3.6-35B",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8080",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "model_group": "Qwen3.6-35B",
                "hidden_params": {"additional_headers": {}, "litellm_overhead_time_ms": 0},
                "metadata": {},
                "completion_tokens": 10,
                "litellm_call_id": "call-d",
            },
            "litellm_params": {
                "api_base": "http://vllm:8080",
                "metadata": {"model_info": {"id": "abc-123"}},
            },
        },
        start_time=start,
        end_time=end,
        enum_values=enum_values,
        output_tokens=10.0,
    )
    assert _gauge_value(isolated_registry) == 0.0


# ---------------------------------------------------------------------------
# Leak mode 1: router retries re-inc but terminal events fire once.
# An admit keyed by litellm_call_id must be idempotent; a repeated release
# must be a no-op (never negative).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_same_call_id_admits_once(logger, isolated_registry):
    """Three pre-call-hook fires for one litellm_call_id (2 retries) count as ONE in-flight request."""
    kwargs = {
        "model": "Qwen3.6-35B",
        "messages": [],
        "litellm_call_id": "call-1",
        "litellm_params": {"api_base": "http://vllm:8000", "custom_llm_provider": "hosted_vllm"},
        "standard_logging_object": {
            "model_id": "abc-123",
            "api_base": "http://vllm:8000",
            "model": "Qwen3.6-35B",
            "custom_llm_provider": "hosted_vllm",
            "litellm_call_id": "call-1",
        },
    }
    for _ in range(3):
        await logger.async_pre_call_deployment_hook(kwargs=kwargs, call_type=None)
    assert _gauge_value(isolated_registry) == 1.0

    logger.set_llm_deployment_failure_metrics(
        {
            "model": "Qwen3.6-35B",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "model_group": "Qwen3.6-35B",
                "litellm_call_id": "call-1",
            },
            "litellm_params": {"custom_llm_provider": "hosted_vllm", "api_base": "http://vllm:8000"},
            "exception": Exception("timeout"),
        }
    )
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_repeated_release_never_goes_negative(logger, isolated_registry):
    """Terminal events can fire more than once per admit (dedup gate races); the count must clamp at 0."""
    kwargs = {
        "model": "Qwen3.6-35B",
        "messages": [],
        "litellm_call_id": "call-1",
        "litellm_params": {"api_base": "http://vllm:8000"},
        "standard_logging_object": {
            "model_id": "abc-123",
            "api_base": "http://vllm:8000",
            "model": "Qwen3.6-35B",
            "custom_llm_provider": "hosted_vllm",
            "litellm_call_id": "call-1",
        },
    }
    await logger.async_pre_call_deployment_hook(kwargs=kwargs, call_type=None)

    failure_kwargs = {
        "model": "Qwen3.6-35B",
        "standard_logging_object": {
            "model_id": "abc-123",
            "api_base": "http://vllm:8000",
            "model": "Qwen3.6-35B",
            "custom_llm_provider": "hosted_vllm",
            "model_group": "Qwen3.6-35B",
            "litellm_call_id": "call-1",
        },
        "litellm_params": {"api_base": "http://vllm:8000"},
        "exception": Exception("timeout"),
    }
    for _ in range(3):
        logger.set_llm_deployment_failure_metrics(failure_kwargs)
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_inc_skipped_without_call_id(logger, isolated_registry):
    """No litellm_call_id means the admit cannot be deduped; skipping is safer than double-counting."""
    kwargs = {
        "model": "Qwen3.6-35B",
        "messages": [],
        "litellm_params": {"api_base": "http://vllm:8000", "custom_llm_provider": "hosted_vllm"},
        "standard_logging_object": {
            "model_id": "abc-123",
            "api_base": "http://vllm:8000",
            "model": "Qwen3.6-35B",
            "custom_llm_provider": "hosted_vllm",
        },
    }
    await logger.async_pre_call_deployment_hook(kwargs=kwargs, call_type=None)
    assert _gauge_value(isolated_registry) == 0.0


# ---------------------------------------------------------------------------
# Leak mode 2: a terminal event that never fires must be healed by TTL
# eviction at scrape time (the hamsa-stt prod case: 644 frozen forever).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evict_expired_heals_abandoned_entry(logger, isolated_registry):
    """An entry whose dec never fired is dropped once it exceeds the TTL."""
    kwargs = {
        "model": "Qwen3.6-35B",
        "messages": [],
        "litellm_call_id": "call-abandoned",
        "litellm_params": {"api_base": "http://vllm:8000", "custom_llm_provider": "hosted_vllm"},
        "standard_logging_object": {
            "model_id": "abc-123",
            "api_base": "http://vllm:8000",
            "model": "Qwen3.6-35B",
            "custom_llm_provider": "hosted_vllm",
            "litellm_call_id": "call-abandoned",
        },
    }
    await logger.async_pre_call_deployment_hook(kwargs=kwargs, call_type=None)
    assert _gauge_value(isolated_registry) == 1.0

    # no dec ever fires; age the entry past the TTL
    entries = logger._deployment_in_flight_ledger._entries["abc-123"]
    entries["call-abandoned"] = time.monotonic() - 3600.0

    logger.evict_stale_deployment_in_flight()
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_evict_keeps_live_entries(logger, isolated_registry):
    """Eviction must not drop requests that are genuinely still in flight."""
    kwargs = {
        "model": "Qwen3.6-35B",
        "messages": [],
        "litellm_call_id": "call-live",
        "litellm_params": {"api_base": "http://vllm:8000", "custom_llm_provider": "hosted_vllm"},
        "standard_logging_object": {
            "model_id": "abc-123",
            "api_base": "http://vllm:8000",
            "model": "Qwen3.6-35B",
            "custom_llm_provider": "hosted_vllm",
            "litellm_call_id": "call-live",
        },
    }
    await logger.async_pre_call_deployment_hook(kwargs=kwargs, call_type=None)
    logger.evict_stale_deployment_in_flight()
    assert _gauge_value(isolated_registry) == 1.0


# ---------------------------------------------------------------------------
# Leak mode 3: failure logging with a partially-populated payload must not
# raise before the metrics block (the STT prod case: zero counters recorded).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failure_logging_with_missing_metadata_block(logger, isolated_registry):
    """A failure payload with no metadata block must still dec and record the failure counter."""
    await logger.async_pre_call_deployment_hook(
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_call_id": "call-stt",
            "litellm_params": {"api_base": "http://vllm:8000", "custom_llm_provider": "hamsa"},
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hamsa",
                "litellm_call_id": "call-stt",
            },
        },
        call_type=None,
    )
    assert _gauge_value(isolated_registry) == 1.0

    logger.set_llm_deployment_failure_metrics(
        {
            "model": "Qwen3.6-35B",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hamsa",
                "model_group": "Qwen3.6-35B",
                "litellm_call_id": "call-stt",
                "metadata": {},
            },
            "litellm_params": {"custom_llm_provider": "hamsa", "api_base": "http://vllm:8000"},
            "exception": Exception("upstream 502"),
        }
    )
    assert _gauge_value(isolated_registry) == 0.0

    output = generate_latest(isolated_registry).decode()
    failure_lines = [
        line
        for line in output.splitlines()
        if line.startswith("litellm_deployment_failure_responses") and 'model_id="abc-123"' in line
    ]
    assert failure_lines, "failure counter must be recorded even with an empty metadata block"


# ---------------------------------------------------------------------------
# Ledger unit tests: idempotency, clamping and eviction invariants.
# ---------------------------------------------------------------------------


def test_ledger_admit_is_idempotent_per_call_id():
    ledger = DeploymentInFlightLedger()
    emitted: list[tuple[tuple[str, str, str, str], int]] = []

    def emit(labels, value):
        emitted.append((labels, value))

    now = time.monotonic()
    ledger.admit("m1", "c1", now, "model-a", "http://a:8000", "hosted_vllm", emit)
    ledger.admit("m1", "c1", now + 100.0, "model-a", "http://a:8000", "hosted_vllm", emit)
    ledger.admit("m1", "c2", now + 50.0, "model-a", "http://a:8000", "hosted_vllm", emit)

    values = [v for _, v in emitted]
    assert values[-1] == 2, "two distinct call ids must count 2"
    assert max(values) == 2, "repeated admit of the same call id must not raise the count"


def test_ledger_release_clamps_at_zero():
    ledger = DeploymentInFlightLedger()
    emitted: list[tuple[tuple[str, str, str, str], int]] = []

    def emit(labels, value):
        emitted.append((labels, value))

    ledger.release("m1", "never-admitted", emit)
    assert emitted == [], "release without a prior admit emits nothing"

    ledger.admit("m1", "c1", time.monotonic(), "model-a", "http://a:8000", "hosted_vllm", emit)
    ledger.release("m1", "c1", emit)
    ledger.release("m1", "c1", emit)
    values = [v for _, v in emitted]
    assert values[-1] == 0, "release after release must stay at 0"


def test_ledger_labels_assigned_once_at_admit():
    """The dec paths never re-derive labels, so an admit with drifted labels on a later
    attempt must keep the canonical series of the first admit, not fork a new one."""
    ledger = DeploymentInFlightLedger()
    emitted: dict[tuple[str, str, str, str], int] = {}

    def emit(labels, value):
        emitted[labels] = value

    now = time.monotonic()
    ledger.admit("m1", "c1", now, "model-a", "http://a:8000", "hosted_vllm", emit)
    ledger.admit("m1", "c1", now + 1.0, "model-a-different-name", "http://b:9000", "other", emit)
    ledger.release("m1", "c1", emit)

    live = {labels: v for labels, v in emitted.items() if v != 0}
    assert live == {}, "all series must be reset to 0 after release"
    assert len(emitted) == 1, "no second series may be created by a re-admit with drifted labels"


@pytest.mark.asyncio
async def test_inc_dec_normalize_api_base_endpoint_suffix_mismatch(logger, isolated_registry):
    """Regression: litellm_params.api_base is mutated between pre-call and success.

    At pre-call, api_base is the full endpoint URL (e.g. .../v1/chat/completions).
    By the time the success hook runs, litellm has stripped it to the base URL
    (.../v1).  Without normalization the inc and dec hit different label series
    and the gauge leaks forever (goes negative).

    This test reproduces the production bug: inc with /chat/completions suffix,
    dec without it.  The gauge must still return to 0.
    """
    await logger.async_pre_call_deployment_hook(
        kwargs={
            "model": "zai-org/GLM-5.2-FP8",
            "messages": [],
            "litellm_call_id": "call-suffix",
            "litellm_params": {
                "api_base": "http://vllm:8080/v1/chat/completions",
                "custom_llm_provider": "hosted_vllm",
            },
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8080/v1/chat/completions",
                "model": "zai-org/GLM-5.2-FP8",
                "custom_llm_provider": "hosted_vllm",
                "litellm_call_id": "call-suffix",
            },
        },
        call_type=None,
    )
    assert _gauge_value(isolated_registry) == 1.0

    start = datetime(2026, 1, 1, 0, 0, 0)
    end = datetime(2026, 1, 1, 0, 0, 5)
    enum_values = UserAPIKeyLabelValues(
        litellm_model_name="zai-org/GLM-5.2-FP8",
        model_id="abc-123",
        api_base="http://vllm:8080/v1",
        api_provider="hosted_vllm",
    )
    logger.set_llm_deployment_success_metrics(
        request_kwargs={
            "model": "zai-org/GLM-5.2-FP8",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8080/v1",
                "model": "zai-org/GLM-5.2-FP8",
                "custom_llm_provider": "hosted_vllm",
                "model_group": "zai-org/GLM-5.2-FP8",
                "hidden_params": {"additional_headers": {}, "litellm_overhead_time_ms": 0},
                "metadata": {},
                "completion_tokens": 10,
                "litellm_call_id": "call-suffix",
            },
            "litellm_params": {
                "api_base": "http://vllm:8080/v1",
                "metadata": {"model_info": {"id": "abc-123"}},
            },
        },
        start_time=start,
        end_time=end,
        enum_values=enum_values,
        output_tokens=10.0,
    )
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_inc_dec_normalize_api_base_failure_path(logger, isolated_registry):
    """Same regression as above but for the failure dec path."""
    await logger.async_pre_call_deployment_hook(
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_call_id": "call-suffix-failure",
            "litellm_params": {
                "api_base": "http://vllm:8080/v1/chat/completions",
                "custom_llm_provider": "hosted_vllm",
            },
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8080/v1/chat/completions",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "litellm_call_id": "call-suffix-failure",
            },
        },
        call_type=None,
    )
    assert _gauge_value(isolated_registry) == 1.0

    logger.set_llm_deployment_failure_metrics(
        {
            "model": "Qwen3.6-35B",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8080/v1",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "model_group": "Qwen3.6-35B",
                "litellm_call_id": "call-suffix-failure",
            },
            "litellm_params": {"api_base": "http://vllm:8080/v1", "custom_llm_provider": "hosted_vllm"},
            "exception": Exception("timeout"),
        }
    )
    assert _gauge_value(isolated_registry) == 0.0


def test_normalize_api_base_strips_known_suffixes():
    assert normalize_api_base_for_gauge("http://vllm:8080/v1/chat/completions") == "http://vllm:8080/v1"
    assert normalize_api_base_for_gauge("http://vllm:8080/v1/embeddings") == "http://vllm:8080/v1"
    assert normalize_api_base_for_gauge("http://vllm:8080/v1/responses") == "http://vllm:8080/v1"
    assert normalize_api_base_for_gauge("http://vllm:8080/v1/") == "http://vllm:8080/v1"
    assert normalize_api_base_for_gauge("http://vllm:8080/v1") == "http://vllm:8080/v1"
    assert normalize_api_base_for_gauge("") == ""
    assert normalize_api_base_for_gauge("http://vllm:8080/custom/path") == "http://vllm:8080/custom/path"


@pytest.mark.asyncio
async def test_inc_dec_model_name_label_match_with_provider_prefix(logger, isolated_registry):
    """Regression: at async_pre_call_deployment_hook time the model string still
    contains the provider prefix (e.g. "hosted_vllm/zai-org/GLM-5.2-FP8") and
    custom_llm_provider is empty.  By the time the success hook runs,
    function_setup has stripped the prefix so request_kwargs["model"] is
    "zai-org/GLM-5.2-FP8".  But standard_logging_payload["model"] is
    reconstructed with the prefix via reconstruct_model_name().

    The INC path uses the raw kwargs["model"] (prefixed).  The DEC path must
    use standard_logging_payload["model"] (also prefixed) so the labels match.
    If DEC used request_kwargs["model"] (unprefixed), the labels would differ
    and the gauge would leak forever.

    This test reproduces the production bug: inc with prefixed model, dec with
    standard_logging_payload["model"] also prefixed.  The gauge must return to 0.
    """
    await logger.async_pre_call_deployment_hook(
        kwargs={
            "model": "hosted_vllm/zai-org/GLM-5.2-FP8",
            "messages": [],
            "litellm_call_id": "call-prefix",
            "metadata": {
                "model_info": {"id": "abc-123"},
                "api_base": "http://vllm:8080/v1",
                "custom_llm_provider": "",
            },
        },
        call_type=None,
    )
    assert _gauge_value(isolated_registry) == 1.0

    start = datetime(2026, 1, 1, 0, 0, 0)
    end = datetime(2026, 1, 1, 0, 0, 5)
    enum_values = UserAPIKeyLabelValues(
        litellm_model_name="zai-org/GLM-5.2-FP8",
        model_id="abc-123",
        api_base="http://vllm:8080/v1",
        api_provider="hosted_vllm",
    )
    logger.set_llm_deployment_success_metrics(
        request_kwargs={
            "model": "zai-org/GLM-5.2-FP8",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8080/v1",
                "model": "hosted_vllm/zai-org/GLM-5.2-FP8",
                "custom_llm_provider": "hosted_vllm",
                "model_group": "zai-org/GLM-5.2-FP8",
                "hidden_params": {"additional_headers": {}, "litellm_overhead_time_ms": 0},
                "metadata": {},
                "completion_tokens": 10,
                "litellm_call_id": "call-prefix",
            },
            "litellm_params": {
                "api_base": "http://vllm:8080/v1",
                "custom_llm_provider": "hosted_vllm",
                "metadata": {"model_info": {"id": "abc-123"}},
            },
        },
        start_time=start,
        end_time=end,
        enum_values=enum_values,
        output_tokens=10.0,
    )
    assert _gauge_value(isolated_registry) == 0.0


def _gauge_series_by(registry, model_id):
    """Return {metric_name_line: value} for every in-progress series of model_id."""
    output = generate_latest(registry).decode()
    series = {}
    for line in output.splitlines():
        if "litellm_deployment_in_progress_requests" not in line or f'model_id="{model_id}"' not in line:
            continue
        metric, _, val = line.rpartition(" ")
        series[metric] = float(val)
    return series


@pytest.mark.asyncio
async def test_label_divergence_self_heals_no_phantom_one(logger, isolated_registry):
    """Regression for the production leak: inc and dec derive labels from
    different sources, so they can land on *different* series for the same
    model_id (e.g. inc with api_base set, dec without api_base).

    Before the ledger fix, the +1 series stayed stuck forever (uncalled model
    showed 1); the -1 series lingered at -1. The ledger keys by model_id and
    reconciles every stale series back to 0, so the gauge must return to 0
    and no phantom series may remain.
    """
    await logger.async_pre_call_deployment_hook(
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_call_id": "call-divergence",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "litellm_call_id": "call-divergence",
            },
        },
        call_type=None,
    )
    assert _gauge_value(isolated_registry) == 1.0

    start = datetime(2026, 1, 1, 0, 0, 0)
    end = datetime(2026, 1, 1, 0, 0, 5)
    enum_values = UserAPIKeyLabelValues(
        litellm_model_name="Qwen3.6-35B",
        model_id="abc-123",
        api_base="",
        api_provider="hosted_vllm",
    )
    logger.set_llm_deployment_success_metrics(
        request_kwargs={
            "model": "Qwen3.6-35B",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "model_group": "Qwen3.6-35B",
                "hidden_params": {"additional_headers": {}, "litellm_overhead_time_ms": 0},
                "metadata": {},
                "completion_tokens": 10,
                "litellm_call_id": "call-divergence",
            },
            "litellm_params": {
                "custom_llm_provider": "hosted_vllm",
                "api_base": "",
                "metadata": {"model_info": {"id": "abc-123"}},
            },
        },
        start_time=start,
        end_time=end,
        enum_values=enum_values,
        output_tokens=10.0,
    )
    assert _gauge_value(isolated_registry) == 0.0
    # No phantom nonzero series may remain for this model_id. The label series
    # stays registered (prometheus does not GC labels) but its value must be 0.
    assert set(_gauge_series_by(isolated_registry, "abc-123").values()) == {0.0}


@pytest.mark.asyncio
async def test_gauge_clamps_at_zero_when_dec_outnumbers_inc(logger, isolated_registry):
    """The reconciled gauge must never go negative, even if a stray dec fires
    (e.g. a retry/exception path decs a request whose inc was attributed to a
    different worker)."""
    start = datetime(2026, 1, 1, 0, 0, 0)
    end = datetime(2026, 1, 1, 0, 0, 5)
    enum_values = UserAPIKeyLabelValues(
        litellm_model_name="Qwen3.6-35B",
        model_id="abc-123",
        api_base="http://vllm:8000",
        api_provider="hosted_vllm",
    )
    logger.set_llm_deployment_success_metrics(
        request_kwargs={
            "model": "Qwen3.6-35B",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
                "model_group": "Qwen3.6-35B",
                "hidden_params": {"additional_headers": {}, "litellm_overhead_time_ms": 0},
                "metadata": {},
                "completion_tokens": 10,
            },
            "litellm_params": {
                "custom_llm_provider": "hosted_vllm",
                "api_base": "http://vllm:8000",
                "metadata": {"model_info": {"id": "abc-123"}},
            },
        },
        start_time=start,
        end_time=end,
        enum_values=enum_values,
        output_tokens=10.0,
    )
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_success_dec_falls_back_to_standard_logging_model_id(logger, isolated_registry):
    """Regression: success dec must not leak when metadata.model_info is absent.

    The inc path resolves model_id from standard_logging_object["model_id"]
    (with fallback to metadata.model_info.id). The failure dec path does the
    same. But the success dec historically resolved model_id ONLY from
    metadata.model_info.id. For request types (e.g. streaming STT/TTS) where
    metadata.model_info is not populated at success time while
    standard_logging_object["model_id"] IS set, the dec no-ops on `if model_id:`
    and the gauge leaks forever.

    This reproduces the production leak where an idle STT deployment froze at a
    phantom concurrency of 381 on every proxy pod with zero live traffic. The
    gauge must return to 0.
    """
    await logger.async_pre_call_deployment_hook(
        kwargs={
            "model": "hamsa/hamsa-stt",
            "messages": [],
            "litellm_call_id": "call-stt-fallback",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://stt:8080",
                "model": "hamsa/hamsa-stt",
                "custom_llm_provider": "hosted_vllm",
                "litellm_call_id": "call-stt-fallback",
            },
        },
        call_type=None,
    )
    assert _gauge_value(isolated_registry) == 1.0

    start = datetime(2026, 1, 1, 0, 0, 0)
    end = datetime(2026, 1, 1, 0, 0, 5)
    enum_values = UserAPIKeyLabelValues(
        litellm_model_name="hamsa/hamsa-stt",
        model_id="abc-123",
        api_base="http://stt:8080",
        api_provider="hosted_vllm",
    )
    # NOTE: litellm_params.metadata.model_info is intentionally absent. The dec
    # must fall back to standard_logging_object["model_id"] like the inc path.
    logger.set_llm_deployment_success_metrics(
        request_kwargs={
            "model": "hamsa/hamsa-stt",
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://stt:8080",
                "model": "hamsa/hamsa-stt",
                "custom_llm_provider": "hosted_vllm",
                "model_group": "hamsa/hamsa-stt",
                "hidden_params": {"additional_headers": {}, "litellm_overhead_time_ms": 0},
                "metadata": {},
                "completion_tokens": 10,
                "litellm_call_id": "call-stt-fallback",
            },
            "litellm_params": {
                "custom_llm_provider": "hosted_vllm",
                "api_base": "http://stt:8080",
            },
        },
        start_time=start,
        end_time=end,
        enum_values=enum_values,
        output_tokens=10.0,
    )
    assert _gauge_value(isolated_registry) == 0.0


# ---------------------------------------------------------------------------
# Per-worker sweeper: each granian worker self-heals its own stale entries
# without depending on a scrape being routed to it.
# ---------------------------------------------------------------------------


class _StopSweeper(Exception):
    """Sentinel used to break out of the otherwise-infinite sweeper loop in tests."""


def test_sweeper_state_starts_once_per_process():
    """ensure_in_flight_sweeper_started must be idempotent: one thread per process."""
    from litellm.integrations.prometheus_helpers import deployment_in_flight as slice_mod

    original_state = slice_mod._sweeper_state.started
    try:
        slice_mod._sweeper_state.started = False
        with patch.object(slice_mod.threading, "Thread") as mock_thread:
            slice_mod.ensure_in_flight_sweeper_started()
            slice_mod.ensure_in_flight_sweeper_started()
            slice_mod.ensure_in_flight_sweeper_started()
        assert mock_thread.call_count == 1, "the sweeper must start exactly once per process"
        assert mock_thread.call_args.kwargs.get("daemon") is True, "sweeper must be a daemon thread"
    finally:
        slice_mod._sweeper_state.started = original_state


def test_admit_starts_the_sweeper(logger, isolated_registry):
    """The first admit in a process must start the per-worker sweeper."""
    from litellm.integrations.prometheus_helpers import deployment_in_flight as slice_mod

    original_state = slice_mod._sweeper_state.started
    try:
        slice_mod._sweeper_state.started = False
        with patch.object(slice_mod.threading, "Thread") as mock_thread:
            logger._admit_deployment_in_flight(
                model_id="abc-123",
                litellm_model_name="Qwen3.6-35B",
                api_base="http://vllm:8000",
                api_provider="hosted_vllm",
                call_id="call-sweeper",
            )
        assert mock_thread.call_count == 1
    finally:
        slice_mod._sweeper_state.started = original_state


def test_sweeper_loop_evicts_each_tick():
    """The sweeper loop must evict on each tick and keep ticking."""
    from litellm.integrations.prometheus_helpers import deployment_in_flight as slice_mod

    evictions: list[int] = []

    def fake_evict():
        evictions.append(1)
        if len(evictions) >= 2:
            raise _StopSweeper

    with patch.object(slice_mod, "evict_all_prometheus_loggers", side_effect=fake_evict), patch.object(
        slice_mod.time, "sleep", return_value=None
    ):
        with pytest.raises(_StopSweeper):
            slice_mod._sweeper_loop()

    assert len(evictions) == 2, "the loop must evict on every tick"


def test_sweeper_interval_is_under_the_ttl():
    """The sweep interval must stay below the TTL, or self-healing would not bound staleness."""
    from litellm.integrations.prometheus_helpers import deployment_in_flight as slice_mod

    assert 0 < slice_mod._IN_FLIGHT_SWEEP_INTERVAL_SECONDS < slice_mod._IN_FLIGHT_TTL_SECONDS


def test_evict_all_prometheus_loggers_swallows_errors():
    """A failing eviction must never propagate (it runs on a background thread)."""
    from litellm.integrations.prometheus_helpers import deployment_in_flight as slice_mod

    class BoomLogger:
        def evict_stale_deployment_in_flight(self):
            raise RuntimeError("boom")

    with patch.object(litellm.logging_callback_manager, "get_custom_loggers_for_type", return_value=[BoomLogger()]):
        slice_mod.evict_all_prometheus_loggers()


def test_evict_all_prometheus_loggers_evicts_each_logger():
    """Every live Prometheus logger must be swept, not just the first."""
    from litellm.integrations.prometheus_helpers import deployment_in_flight as slice_mod

    class RecordingLogger:
        def __init__(self):
            self.calls = 0

        def evict_stale_deployment_in_flight(self):
            self.calls += 1

    a, b = RecordingLogger(), RecordingLogger()
    with patch.object(litellm.logging_callback_manager, "get_custom_loggers_for_type", return_value=[a, b]):
        slice_mod.evict_all_prometheus_loggers()
    assert a.calls == 1 and b.calls == 1
