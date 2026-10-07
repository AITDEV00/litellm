"""Tests for the OpenRouter endpoints route (design §31, checklist Steps 13-17).

Regression focus: the response is the official ``ListEndpointsResponse`` (not a
bespoke dict), each deployment becomes one endpoint, unobservable statistics
stay null, and ``gateway_status`` reflects OICM lifecycle with freshness judged
per source rather than per model.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from openrouter.components.listendpointsresponse import ListEndpointsResponse

from litellm.proxy.openrouter_compat.domain.architecture import ModelArchitecture
from litellm.proxy.openrouter_compat.domain.capabilities import (
    ApiCapabilities,
    ModelCapabilities,
)
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.domain.identity import ModelIdentity
from litellm.proxy.openrouter_compat.domain.limits import ModelLimits
from litellm.proxy.openrouter_compat.domain.logical_model import AggregatedModel
from litellm.proxy.openrouter_compat.domain.provenance import (
    ModelProvenance,
    RuntimeInfo,
)
from litellm.proxy.openrouter_compat.enrichment.pricing import Pricing, PricingResolver
from litellm.proxy.openrouter_compat.gateway_status import (
    GatewayStateResolver,
    GatewayStatusInputs,
)
from litellm.proxy.openrouter_compat.mapping.endpoints import OpenRouterEndpointsMapper

_NOW = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)


def _deployment(deployment_id: str, *, kind: str = "sglang") -> DiscoveredDeploymentModel:
    return DiscoveredDeploymentModel(
        identity=ModelIdentity(
            logical_model_name="Qwen/Qwen3.6-35B-A3B-FP8",
            upstream_model_id="Qwen/Qwen3.6-35B-A3B-FP8",
            display_name="Qwen3.6 35B",
            created=1791369763,
        ),
        limits=ModelLimits(
            context_length=262144,
            max_input_tokens=200000,
            max_completion_tokens=32768,
        ),
        architecture=ModelArchitecture(model_type="qwen3"),
        capabilities=ModelCapabilities(input_modalities={"text"}, output_modalities={"text"}),
        api_capabilities=ApiCapabilities(chat_completions=True),
        runtime=RuntimeInfo(kind=kind, deployment_id=deployment_id),
        provenance=ModelProvenance(),
    )


def _aggregated(deployments: list[DiscoveredDeploymentModel]) -> AggregatedModel:
    return AggregatedModel(
        logical_model_name="Qwen/Qwen3.6-35B-A3B-FP8",
        deployments=deployments,
        identity=deployments[0].identity,
        limits=deployments[0].limits,
        architecture=deployments[0].architecture,
        capabilities=deployments[0].capabilities,
    )


class _FixedPricing(PricingResolver):
    def resolve_for_deployments(self, deployments):
        return Pricing(prompt="0.25", completion="1.5")


def _mapper() -> OpenRouterEndpointsMapper:
    return OpenRouterEndpointsMapper(pricing_resolver=_FixedPricing())


def _payload(model: AggregatedModel, statuses: dict[str, object]) -> dict[str, object]:
    """Serialize the route payload exactly as FastAPI would."""
    return _mapper().map_endpoints(
        model, public_id="Qwen/Qwen3.6-35B-A3B-FP8", statuses=statuses  # type: ignore[arg-type]  # resolver-typed
    )


def _statuses(model: AggregatedModel, **overrides: object) -> dict[str, object]:
    inputs = {
        "oicm_status": "Ready",
        "serving_available": True,
        "replicas_desired": 1,
        "replicas_available": 1,
        "observed_at": _NOW.isoformat(),
        "cluster": "alain",
        "health_status": "healthy",
        "source_checked_at": _NOW,
    }
    inputs.update(overrides)
    resolver = GatewayStateResolver(now=lambda: _NOW)
    return {
        deployment.runtime.deployment_id: resolver.resolve(GatewayStatusInputs(**inputs))  # type: ignore[arg-type]
        for deployment in model.deployments
    }


def test_response_is_official_list_endpoints_response():
    """The payload must parse as the official SDK model, not a bespoke dict."""
    model = _aggregated([_deployment("dep-1")])
    payload = _payload(model, _statuses(model))

    parsed = ListEndpointsResponse.model_validate(payload["data"])
    assert parsed.id == "Qwen/Qwen3.6-35B-A3B-FP8"
    assert parsed.name == "Qwen3.6 35B"
    assert len(parsed.endpoints) == 1


def test_one_endpoint_per_deployment():
    """A logical model with two deployments advertises two endpoints."""
    model = _aggregated([_deployment("dep-1"), _deployment("dep-2")])
    payload = _payload(model, _statuses(model))

    assert len(payload["data"]["endpoints"]) == 2


def test_unobservable_statistics_are_null_not_fabricated():
    """Latency/uptime have no honest source yet, so they must be null."""
    model = _aggregated([_deployment("dep-1")])
    endpoint = _payload(model, _statuses(model))["data"]["endpoints"][0]

    assert endpoint["latency_last_30m"] is None
    assert endpoint["throughput_last_30m"] is None
    assert endpoint["uptime_last_5m"] is None
    assert endpoint["uptime_last_30m"] is None
    assert endpoint["uptime_last_1d"] is None


def test_endpoint_carries_limits_pricing_and_provider():
    model = _aggregated([_deployment("dep-1")])
    endpoint = _payload(model, _statuses(model))["data"]["endpoints"][0]

    assert endpoint["context_length"] == 262144
    assert endpoint["max_prompt_tokens"] == 200000
    assert endpoint["max_completion_tokens"] == 32768
    assert endpoint["pricing"] == {"prompt": "0.25", "completion": "1.5"}
    assert endpoint["provider_name"] == "sglang"
    assert endpoint["tag"] == "sglang"
    assert endpoint["model_id"] == "Qwen/Qwen3.6-35B-A3B-FP8"


def test_internal_paths_and_secrets_never_appear():
    """No api_base, fingerprint, or auth header may leak into the payload."""
    deployment = _deployment("dep-1")
    deployment = deployment.model_copy(
        update={"runtime": deployment.runtime.model_copy(update={"api_base_fingerprint": "deadbeef"})}
    )
    model = _aggregated([deployment])
    serialized = json.dumps(_payload(model, _statuses(model)))

    assert "deadbeef" not in serialized
    assert "api_base" not in serialized
    assert "Authorization" not in serialized


@pytest.mark.parametrize(
    ("oicm_status", "serving_available", "expected_availability", "expected_lifecycle"),
    [
        ("Ready", True, "online", "stable"),
        ("Ready", False, "degraded", "stable"),
        ("Available", True, "online", "stable"),
        ("Stopped", False, "offline", "stopped"),
        ("Deploying", False, "offline", "deploying"),
        ("Pending", False, "offline", "deploying"),
        ("Failed", False, "offline", "failed"),
        (None, None, "unknown", "unknown"),
    ],
)
def test_lifecycle_transition_matrix(
    oicm_status: str | None,
    serving_available: bool | None,
    expected_availability: str,
    expected_lifecycle: str,
):
    """Each OICM lifecycle maps to exactly one gateway verdict.

    ``Ready`` with ``serving_available=False`` is the load-bearing case: OICM
    still calls the deployment ready while its pods serve nothing.
    """
    resolver = GatewayStateResolver(now=lambda: _NOW)
    status = resolver.resolve(
        GatewayStatusInputs(
            oicm_status=oicm_status,
            serving_available=serving_available,
            replicas_desired=1,
            replicas_available=1 if serving_available else 0,
            observed_at=_NOW.isoformat(),
            cluster="alain",
            health_status="healthy" if serving_available else "unhealthy",
            source_checked_at=_NOW,
        )
    )

    assert status.availability == expected_availability
    assert status.lifecycle == expected_lifecycle


def test_stale_source_forces_unknown_even_when_last_known_status_was_ready():
    """A dead heartbeat must not keep reporting a confident 'online'."""
    resolver = GatewayStateResolver(now=lambda: _NOW)
    status = resolver.resolve(
        GatewayStatusInputs(
            oicm_status="Ready",
            serving_available=True,
            replicas_desired=1,
            replicas_available=1,
            observed_at=(_NOW - timedelta(hours=2)).isoformat(),
            cluster="alain",
            health_status="healthy",
            source_checked_at=_NOW - timedelta(seconds=91),
        )
    )

    assert status.stale is True
    assert status.availability == "unknown"
    assert status.lifecycle == "stable"


def test_missing_heartbeat_is_stale():
    """No source row at all means nobody is watching, so nothing is fresh."""
    resolver = GatewayStateResolver(now=lambda: _NOW)
    status = resolver.resolve(
        GatewayStatusInputs(
            oicm_status="Ready",
            serving_available=True,
            replicas_desired=1,
            replicas_available=1,
            observed_at=_NOW.isoformat(),
            cluster="alain",
            health_status="healthy",
            source_checked_at=None,
        )
    )

    assert status.stale is True
    assert status.availability == "unknown"


def test_fresh_heartbeat_is_not_stale():
    resolver = GatewayStateResolver(now=lambda: _NOW)
    status = resolver.resolve(
        GatewayStatusInputs(
            oicm_status="Ready",
            serving_available=True,
            replicas_desired=1,
            replicas_available=1,
            observed_at=_NOW.isoformat(),
            cluster="alain",
            health_status="healthy",
            source_checked_at=_NOW - timedelta(seconds=30),
        )
    )

    assert status.stale is False


def test_staleness_uses_source_heartbeat_not_model_observed_at():
    """An hour-old model observation stays fresh while its source is alive."""
    resolver = GatewayStateResolver(now=lambda: _NOW)
    status = resolver.resolve(
        GatewayStatusInputs(
            oicm_status="Ready",
            serving_available=True,
            replicas_desired=1,
            replicas_available=1,
            observed_at=(_NOW - timedelta(hours=1)).isoformat(),
            cluster="alain",
            health_status="healthy",
            source_checked_at=_NOW - timedelta(seconds=30),
        )
    )

    assert status.stale is False
    assert status.availability == "online"


def test_gateway_status_is_attached_per_endpoint():
    model = _aggregated([_deployment("dep-1")])
    endpoint = _payload(model, _statuses(model))["data"]["endpoints"][0]

    assert endpoint["gateway_status"]["availability"] == "online"
    assert endpoint["gateway_status"]["source"] == "alain"
    assert endpoint["gateway_status"]["healthy"] is True
    assert endpoint["gateway_status"]["replicas"] == {"desired": 1, "available": 1}


def test_endpoint_without_status_has_null_gateway_status():
    """An unmanaged deployment is still listed, with an honest null status."""
    model = _aggregated([_deployment("dep-1")])
    endpoint = _payload(model, {})["data"]["endpoints"][0]

    assert endpoint["gateway_status"] is None


def test_empty_response_is_valid_and_has_no_endpoints():
    """A known but undiscovered model must not dead-end its links.details URL."""
    payload = _mapper().map_empty(public_id="litellm/hamsa-tts", logical_model_name="hamsa-tts")

    parsed = ListEndpointsResponse.model_validate(payload["data"])
    assert parsed.id == "litellm/hamsa-tts"
    assert parsed.endpoints == []
