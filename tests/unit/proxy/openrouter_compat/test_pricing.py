"""Tests for OpenRouter-compatible pricing enrichment (design §14).

Regression focus: deployment-explicit model_info pricing must win over the
litellm registry, and unknown pricing must not be silently reported as a
non-zero value.
"""

from __future__ import annotations

import litellm
import pytest

from litellm.proxy.openrouter_compat.domain.architecture import ModelArchitecture
from litellm.proxy.openrouter_compat.domain.capabilities import (
    ApiCapabilities,
    ModelCapabilities,
)
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.domain.identity import ModelIdentity
from litellm.proxy.openrouter_compat.domain.limits import ModelLimits
from litellm.proxy.openrouter_compat.domain.provenance import (
    ModelProvenance,
    RuntimeInfo,
)
from litellm.proxy.openrouter_compat.enrichment.pricing import Pricing, PricingResolver
from litellm.proxy.openrouter_compat.domain.logical_model import AggregatedModel


def _deployment(
    *,
    upstream: str = "upstream-model",
    model_info: dict[str, object] | None = None,
) -> DiscoveredDeploymentModel:
    return DiscoveredDeploymentModel(
        identity=ModelIdentity(
            logical_model_name="logical", upstream_model_id=upstream
        ),
        limits=ModelLimits(context_length=4096),
        architecture=ModelArchitecture(),
        capabilities=ModelCapabilities(),
        api_capabilities=ApiCapabilities(),
        runtime=RuntimeInfo(kind="openai-compatible", deployment_id="dep-1"),
        provenance=ModelProvenance(),
        model_info=model_info or {},
    )


def _logical(deployments: list[DiscoveredDeploymentModel]) -> AggregatedModel:
    return AggregatedModel(
        logical_model_name="logical",
        deployments=deployments,
        identity=deployments[0].identity,
        limits=deployments[0].limits,
        architecture=ModelArchitecture(),
        capabilities=ModelCapabilities(),
        pricing=None,
    )


def test_resolves_deployment_model_info_pricing():
    """Real pricing carried in the deployment model_info must be used."""
    dep = _deployment(
        model_info={"input_cost_per_token": 1.4e-06, "output_cost_per_token": 4.4e-06}
    )
    resolver = PricingResolver()
    assert resolver.resolve(_logical([dep])) == Pricing(
        prompt="1.4", completion="4.4"
    )


def test_deployment_model_info_beats_registry(monkeypatch: pytest.MonkeyPatch):
    """Explicit deployment pricing overrides the litellm registry."""
    dep = _deployment(
        upstream="gpt-4o",
        model_info={"input_cost_per_token": 1e-06, "output_cost_per_token": 3e-06},
    )

    def _registry_fail(model: str) -> dict[str, object]:
        raise litellm.exceptions.NotFoundError(
            message="no model", model=model, llm_provider="openai"
        )

    monkeypatch.setattr(litellm, "get_model_info", _registry_fail)
    resolver = PricingResolver()
    assert resolver.resolve(_logical([dep])) == Pricing(
        prompt="1", completion="3"
    )


def test_registry_fallback_when_no_deployment_pricing(monkeypatch: pytest.MonkeyPatch):
    """Without deployment pricing, fall back to the litellm registry."""
    dep = _deployment(upstream="openai/gpt-4o")

    def _registry(model: str) -> dict[str, object]:
        assert model == "openai/gpt-4o"
        return {"input_cost_per_token": 2.5e-06, "output_cost_per_token": 1e-05}

    monkeypatch.setattr(litellm, "get_model_info", _registry)
    resolver = PricingResolver()
    assert resolver.resolve(_logical([dep])) == Pricing(
        prompt="2.5", completion="10"
    )


def test_unknown_pricing_defaults_to_zero():
    """Unknown pricing under the default free policy reports zero explicitly."""
    dep = _deployment()  # no model_info, upstream not in registry
    resolver = PricingResolver()
    assert resolver.resolve(_logical([dep])) == Pricing(prompt="0", completion="0")


def test_omit_policy_returns_none_for_unknown():
    """The omit policy leaves pricing unset rather than inventing a value."""
    dep = _deployment()
    resolver = PricingResolver(unknown_policy="omit")
    assert resolver.resolve(_logical([dep])) is None