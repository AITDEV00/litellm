"""
Tests for the litellm_deployment_in_progress_requests gauge inc/dec contract.

The gauge must return to 0 after every LLM call completes (success or failure).
A missed decrement causes the gauge to climb forever.

We test the inc/dec contract at three levels:
1. The gauge itself: inc then dec returns to 0; two incs leaves 2
2. async_log_pre_api_call: incs the gauge when model_id present; noop when absent
3. set_llm_deployment_failure_metrics / set_llm_deployment_success_metrics: decs
   the gauge after the call completes
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from prometheus_client import CollectorRegistry, Gauge, Histogram, generate_latest

from litellm.integrations.prometheus import PrometheusLogger
from litellm.types.integrations.prometheus import (
    PrometheusMetricLabels,
    UserAPIKeyLabelValues,
)


@pytest.fixture
def isolated_registry():
    reg = CollectorRegistry()
    yield reg


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
        return pl


def _gauge_value(registry, model_id="abc-123"):
    output = generate_latest(registry).decode()
    for line in output.splitlines():
        if (
            "litellm_deployment_in_progress_requests" in line
            and f'model_id="{model_id}"' in line
        ):
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
        "standard_logging_object": {
            "model_id": "abc-123",
            "api_base": "http://vllm:8000",
            "model": "Qwen3.6-35B",
            "custom_llm_provider": "hosted_vllm",
        },
    }
    await logger.async_log_pre_api_call(
        model="Qwen3.6-35B", messages=[], kwargs=kwargs
    )
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
    await logger.async_log_pre_api_call(
        model="Qwen3.6-35B", messages=[], kwargs=kwargs
    )
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
    await logger.async_log_pre_api_call(
        model="Qwen3.6-35B", messages=[], kwargs=kwargs
    )
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_failure_metrics_decs_gauge(logger, isolated_registry):
    await logger.async_log_pre_api_call(
        model="Qwen3.6-35B",
        messages=[],
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_params": {"api_base": "http://vllm:8000", "custom_llm_provider": "hosted_vllm"},
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
            },
        },
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
            },
            "litellm_params": {"custom_llm_provider": "hosted_vllm", "api_base": "http://vllm:8000"},
            "exception": Exception("timeout"),
        }
    )
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_failure_metrics_no_dec_when_model_id_missing(logger, isolated_registry):
    await logger.async_log_pre_api_call(
        model="Qwen3.6-35B",
        messages=[],
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_params": {"api_base": "http://vllm:8000", "custom_llm_provider": "hosted_vllm"},
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
            },
        },
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
    await logger.async_log_pre_api_call(
        model="Qwen3.6-35B",
        messages=[],
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_params": {"api_base": "http://vllm:8080", "custom_llm_provider": "hosted_vllm"},
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8080",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
            },
        },
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
    await logger.async_log_pre_api_call(
        model="Qwen3.6-35B",
        messages=[],
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_params": {"api_base": "http://vllm:8000"},
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8000",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
            },
        },
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
            },
            "litellm_params": {"api_base": "http://vllm:8000"},
            "exception": Exception("timeout"),
        }
    )
    assert _gauge_value(isolated_registry) == 0.0


@pytest.mark.asyncio
async def test_success_metrics_decs_gauge_when_litellm_params_missing_provider(logger, isolated_registry):
    """Regression: same as failure test but for the success path."""
    await logger.async_log_pre_api_call(
        model="Qwen3.6-35B",
        messages=[],
        kwargs={
            "model": "Qwen3.6-35B",
            "messages": [],
            "litellm_params": {"api_base": "http://vllm:8080"},
            "standard_logging_object": {
                "model_id": "abc-123",
                "api_base": "http://vllm:8080",
                "model": "Qwen3.6-35B",
                "custom_llm_provider": "hosted_vllm",
            },
        },
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
