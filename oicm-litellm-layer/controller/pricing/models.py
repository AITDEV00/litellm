from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PricingEntry:
    key: str
    litellm_provider: str
    mode: str
    input_cost_per_token: float
    output_cost_per_token: float
    has_pricing: bool
    source_url: str


@dataclass(frozen=True, slots=True)
class MatcherCandidate:
    json_key: str
    input_cost_per_token: float
    output_cost_per_token: float
    score: float
    matcher_name: str


@dataclass(frozen=True, slots=True)
class PricingResult:
    input_cost_per_token: float
    output_cost_per_token: float
    matched_keys: tuple[str, ...]
    aggregate_score: float
    strategy: str
