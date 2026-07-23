from typing import Optional

from .models import PricingResult


def pricing_to_params(result: Optional[PricingResult]) -> Optional[dict]:
    if result is None:
        return None
    return {
        "input_cost_per_token": result.input_cost_per_token,
        "output_cost_per_token": result.output_cost_per_token,
    }
