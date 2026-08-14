"""Aggregation policies. Explicit and conservative (design §21)."""

from __future__ import annotations

from enum import Enum

from litellm.proxy.openrouter_compat.domain.limits import ModelLimits


class ContextAggregationPolicy(str, Enum):
    GUARANTEED_MIN = "guaranteed_min"
    MAX_AVAILABLE = "max_available"


def aggregate_context(
    contexts: list[int | None],
    policy: ContextAggregationPolicy = ContextAggregationPolicy.GUARANTEED_MIN,
) -> int | None:
    """Minimum known context (guaranteed) unless MAX_AVAILABLE is chosen.

    Unknown (None) values are ignored; if none are known, result is None.
    """
    known = [c for c in contexts if c is not None]
    if not known:
        return None
    if policy == ContextAggregationPolicy.MAX_AVAILABLE:
        return max(known)
    return min(known)


def aggregate_limits(
    limits: list[ModelLimits],
    context_policy: ContextAggregationPolicy = ContextAggregationPolicy.GUARANTEED_MIN,
) -> ModelLimits:
    """Conservative logical limits across deployments.

    - context_length: min (guaranteed) or max, per policy.
    - max_input_tokens / max_completion_tokens: guaranteed minimum.
    """
    context_length = aggregate_context(
        [limit.context_length for limit in limits], context_policy
    )
    input_known = [limit.max_input_tokens for limit in limits if limit.max_input_tokens is not None]
    completion_known = [
        limit.max_completion_tokens
        for limit in limits
        if limit.max_completion_tokens is not None
    ]
    return ModelLimits(
        context_length=context_length,
        max_input_tokens=min(input_known) if input_known else None,
        max_completion_tokens=min(completion_known) if completion_known else None,
    )


def _guaranteed_bool(values: list[bool | None]) -> bool | None:
    """Guaranteed capability: True only if every known value is True.

    Missing (None) is unknown and ignored. A definite False is a negative, so
    the logical capability is only guaranteed True when all known are True.
    """
    known = [v for v in values if v is not None]
    if not known:
        return None
    return all(known)


def aggregate_bool_capability(
    values: list[bool | None], *, conservative: bool = True
) -> bool | None:
    if not conservative:
        # Any True is enough: advertise if at least one deployment supports it.
        return True if any(v is True for v in values) else None
    return _guaranteed_bool(values)