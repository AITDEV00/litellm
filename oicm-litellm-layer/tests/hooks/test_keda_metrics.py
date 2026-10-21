"""Regression tests for the KEDA concurrent-requests gauge.

Upstream invokes the synchronous ``log_pre_api_call`` (not the async
variant), so the increment path must live there. At that point the only
kwargs are ``model_call_details``, whose routing metadata lives under
``litellm_params.metadata``; the top-level ``metadata`` key only appears on
post-call events. The KEDA ScaledObject queries by OICM uuid, so a gauge
labelled with the model name never triggers autoscaling.
"""

from __future__ import annotations

import asyncio

import pytest

import hooks.keda_metrics as keda
from hooks.keda_metrics import KEDAMetricsCallback

pretend_uuid: str = "9dcd9568-1111-4c2e-9f01-0f8674c2ffff"


def _pre_call_kwargs() -> dict:
    """The exact shape ``Logging.pre_call`` passes to ``log_pre_api_call``."""
    return {
        "litellm_call_id": "call-1",
        "model": "hosted_vllm/Qwen/Qwen3.6",
        "litellm_params": {
            "metadata": {"model_info": {"id": "gw-1", "oicm_uuid": pretend_uuid}},
        },
    }


def _post_call_kwargs() -> dict:
    kwargs = _pre_call_kwargs()
    kwargs["metadata"] = {"model_info": {"id": "gw-1", "oicm_uuid": pretend_uuid}}
    return kwargs


def _gauge_value(model_id: str) -> float:
    for metric in keda.CONCURRENT_REQUESTS.collect():
        for sample in metric.samples:
            if sample.labels.get("model_id") == model_id:
                return sample.value
    raise AssertionError(f"no sample labelled model_id={model_id!r}")


@pytest.fixture(autouse=True)
def _clear_gauge():
    keda.CONCURRENT_REQUESTS.clear()
    keda._uuid_cache.clear()
    yield
    keda.CONCURRENT_REQUESTS.clear()
    keda._uuid_cache.clear()


def test_pre_call_labels_the_gauge_with_the_oicm_uuid() -> None:
    if not keda.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")
    cb = KEDAMetricsCallback()

    cb.log_pre_api_call(
        model="hosted_vllm/Qwen/Qwen3.6", messages=[], kwargs=_pre_call_kwargs()
    )

    assert _gauge_value(pretend_uuid) == 1.0


def test_pre_call_falls_back_to_the_model_name_when_no_uuid_resolves() -> None:
    if not keda.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")
    cb = KEDAMetricsCallback()
    kwargs = _pre_call_kwargs()
    kwargs["litellm_params"]["metadata"] = {}

    cb.log_pre_api_call(model="hosted_vllm/Qwen/Qwen3.6", messages=[], kwargs=kwargs)

    assert _gauge_value("Qwen/Qwen3.6") == 1.0


def test_success_event_decrements_the_same_label_the_inc_used() -> None:
    if not keda.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")
    cb = KEDAMetricsCallback()

    cb.log_pre_api_call(
        model="hosted_vllm/Qwen/Qwen3.6", messages=[], kwargs=_pre_call_kwargs()
    )
    asyncio.run(
        cb.async_log_success_event(
            kwargs=_post_call_kwargs(), response_obj=None, start_time=None, end_time=None
        )
    )

    assert _gauge_value(pretend_uuid) == 0.0


@pytest.mark.asyncio
async def test_failure_event_decrements_the_same_label_the_inc_used() -> None:
    if not keda.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")
    cb = KEDAMetricsCallback()

    cb.log_pre_api_call(
        model="hosted_vllm/Qwen/Qwen3.6", messages=[], kwargs=_pre_call_kwargs()
    )
    await cb.async_log_failure_event(
        kwargs=_post_call_kwargs(), response_obj=None, start_time=None, end_time=None
    )

    assert _gauge_value(pretend_uuid) == 0.0


@pytest.mark.asyncio
async def test_stream_event_accepts_the_upstream_arity() -> None:
    """Upstream calls this with four positional params; a one-param override
    raised TypeError into the logging loop on every streamed request."""
    if not keda.PROMETHEUS_AVAILABLE:
        pytest.skip("prometheus_client not installed")
    cb = KEDAMetricsCallback()

    await cb.async_log_stream_event(
        kwargs=_post_call_kwargs(), response_obj=None, start_time=None, end_time=None
    )
