from typing import Optional

from ..config import PRICING_MATCH_THRESHOLD
from .models import MatcherCandidate, PricingResult


def aggregate(
    candidates: list[MatcherCandidate],
    model_id: str,
    threshold: float = PRICING_MATCH_THRESHOLD,
) -> Optional[PricingResult]:
    if not candidates:
        return None

    best_by_key: dict[str, MatcherCandidate] = {}
    for c in candidates:
        existing = best_by_key.get(c.json_key)
        if existing is None or c.score > existing.score:
            best_by_key[c.json_key] = c

    exact = [c for c in best_by_key.values() if c.score >= 1.0]
    if exact:
        c = exact[0]
        return PricingResult(
            input_cost_per_token=c.input_cost_per_token,
            output_cost_per_token=c.output_cost_per_token,
            matched_keys=(c.json_key,),
            aggregate_score=c.score,
            strategy=c.matcher_name,
        )

    filtered = tuple(c for c in best_by_key.values() if c.score >= threshold)
    if not filtered:
        return None

    if len(filtered) == 1:
        c = filtered[0]
        return PricingResult(
            input_cost_per_token=c.input_cost_per_token,
            output_cost_per_token=c.output_cost_per_token,
            matched_keys=(c.json_key,),
            aggregate_score=c.score,
            strategy=c.matcher_name,
        )

    total_weight = sum(c.score for c in filtered)
    if total_weight <= 0:
        return None

    weighted_input = sum(c.input_cost_per_token * c.score for c in filtered)
    weighted_output = sum(c.output_cost_per_token * c.score for c in filtered)

    return PricingResult(
        input_cost_per_token=weighted_input / total_weight,
        output_cost_per_token=weighted_output / total_weight,
        matched_keys=tuple(sorted(c.json_key for c in filtered)),
        aggregate_score=min(c.score for c in filtered),
        strategy="aggregated",
    )
