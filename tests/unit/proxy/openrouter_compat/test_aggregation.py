"""Tests for OpenRouter-compatible aggregation (design §20-21).

Regression focus: guaranteed-min context, guaranteed (intersection) modalities,
and conservative boolean capability aggregation.
"""

from __future__ import annotations

import pytest

from litellm.proxy.openrouter_compat.aggregation.aggregator import ModelAggregator
from litellm.proxy.openrouter_compat.aggregation.policies import (
    ContextAggregationPolicy,
    aggregate_bool_capability,
    aggregate_context,
    aggregate_limits,
)
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


def _deployment(
    logical: str,
    *,
    context: int | None,
    input_modalities: set[str] | None,
    tool_calling: bool | None,
    upstream: str = "upstream-model",
    max_input: int | None = None,
    max_completion: int | None = None,
) -> DiscoveredDeploymentModel:
    return DiscoveredDeploymentModel(
        identity=ModelIdentity(
            logical_model_name=logical, upstream_model_id=upstream
        ),
        limits=ModelLimits(
            context_length=context,
            max_input_tokens=max_input,
            max_completion_tokens=max_completion,
        ),
        architecture=ModelArchitecture(),
        capabilities=ModelCapabilities(
            input_modalities=input_modalities,
            output_modalities={"text"},
            tool_calling=tool_calling,
        ),
        api_capabilities=ApiCapabilities(),
        runtime=RuntimeInfo(kind="openai-compatible", deployment_id="dep-1"),
        provenance=ModelProvenance(),
    )


def test_aggregate_context_guaranteed_min():
    """Known values aggregate to the minimum; unknown (None) values are ignored."""
    ctx = aggregate_context(
        [262144, 131072, None], ContextAggregationPolicy.GUARANTEED_MIN
    )
    assert ctx == 131072


def test_aggregate_context_max_available():
    ctx = aggregate_context(
        [262144, 131072, None], ContextAggregationPolicy.MAX_AVAILABLE
    )
    assert ctx == 262144


def test_aggregate_limits_guaranteed_min_for_token_limits():
    """max_input/max_completion are guaranteed-min, context follows policy."""
    limits = aggregate_limits(
        [
            ModelLimits(
                context_length=100,
                max_input_tokens=10,
                max_completion_tokens=5,
            ),
            ModelLimits(
                context_length=200,
                max_input_tokens=20,
                max_completion_tokens=8,
            ),
        ],
        ContextAggregationPolicy.MAX_AVAILABLE,
    )
    # context follows MAX_AVAILABLE policy...
    assert limits.context_length == 200
    # ...but max_input/max_completion are always guaranteed-min.
    assert limits.max_input_tokens == 10
    assert limits.max_completion_tokens == 5


@pytest.mark.parametrize(
    ("values", "conservative", "expected"),
    [
        ([True, True, True], True, True),
        # True only if every known value is True.
        ([True, None], True, True),
        ([True, False], True, False),
        ([True, None, False], True, False),
        # All unknown stays unknown.
        ([None, None], True, None),
    ],
)
def test_aggregate_bool_capability(values, conservative, expected):
    assert aggregate_bool_capability(values, conservative=conservative) == expected


def test_aggregate_bool_capability_non_conservative_is_true_if_any_true():
    # Non-conservative (optimistic): True if any known value is True.
    assert aggregate_bool_capability([False, True], conservative=False) is True


def test_aggregator_aggregates_identity_and_deployments():
    d1 = _deployment(
        "gpt-x", context=131072, input_modalities={"text", "image"}, tool_calling=True
    )
    d2 = _deployment(
        "gpt-x", context=262144, input_modalities={"text"}, tool_calling=None
    )
    aggregator = ModelAggregator(
        context_policy=ContextAggregationPolicy.GUARANTEED_MIN, conservative=True
    )
    aggregated = aggregator.aggregate("gpt-x", [d1, d2])

    assert aggregated.identity.logical_model_name == "gpt-x"
    assert aggregated.limits.context_length == 131072
    assert aggregated.capabilities.input_modalities == {"text"}
    assert aggregated.capabilities.tool_calling is True
    assert len(aggregated.deployments) == 2