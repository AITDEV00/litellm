import difflib
from typing import Callable

from .models import MatcherCandidate, PricingEntry
from .normalizer import normalize_model_name, parse_family_and_params, tokenize

Matcher = Callable[[str, dict[str, PricingEntry]], tuple[MatcherCandidate, ...]]


def exact_match(
    normalized_model: str,
    index: dict[str, PricingEntry],
) -> tuple[MatcherCandidate, ...]:
    if not normalized_model:
        return ()

    entry = index.get(normalized_model)
    if entry is None:
        return ()

    return (
        MatcherCandidate(
            json_key=entry.key,
            input_cost_per_token=entry.input_cost_per_token,
            output_cost_per_token=entry.output_cost_per_token,
            score=1.0,
            matcher_name="exact",
        ),
    )


def structured_match(
    normalized_model: str,
    index: dict[str, PricingEntry],
) -> tuple[MatcherCandidate, ...]:
    if not normalized_model:
        return ()

    family, param_count = parse_family_and_params(normalized_model)
    if not family:
        return ()

    exact_param_matches: list[MatcherCandidate] = []
    family_only_matches: list[MatcherCandidate] = []

    for entry in index.values():
        entry_family, entry_params = parse_family_and_params(
            normalize_model_name(entry.key)
        )
        if entry_family != family:
            continue

        if param_count is not None and entry_params == param_count:
            exact_param_matches.append(
                MatcherCandidate(
                    json_key=entry.key,
                    input_cost_per_token=entry.input_cost_per_token,
                    output_cost_per_token=entry.output_cost_per_token,
                    score=0.95,
                    matcher_name="structured",
                )
            )
        elif entry_params is None or param_count is None:
            family_only_matches.append(
                MatcherCandidate(
                    json_key=entry.key,
                    input_cost_per_token=entry.input_cost_per_token,
                    output_cost_per_token=entry.output_cost_per_token,
                    score=0.70,
                    matcher_name="structured",
                )
            )

    if exact_param_matches:
        return tuple(exact_param_matches)
    return tuple(family_only_matches)


def fuzzy_match(
    normalized_model: str,
    index: dict[str, PricingEntry],
) -> tuple[MatcherCandidate, ...]:
    if not normalized_model:
        return ()

    model_tokens = set(tokenize(normalized_model))
    if not model_tokens:
        return ()

    candidates: list[MatcherCandidate] = []
    for entry in index.values():
        norm_key = normalize_model_name(entry.key)
        key_tokens = set(tokenize(norm_key))
        if not model_tokens & key_tokens:
            continue

        ratio = difflib.SequenceMatcher(
            None, normalized_model, norm_key
        ).ratio()
        if ratio >= 0.80:
            candidates.append(
                MatcherCandidate(
                    json_key=entry.key,
                    input_cost_per_token=entry.input_cost_per_token,
                    output_cost_per_token=entry.output_cost_per_token,
                    score=ratio,
                    matcher_name="fuzzy",
                )
            )

    return tuple(candidates)


MIN_SUBSTRING_LENGTH = 5


def substring_match(
    normalized_model: str,
    index: dict[str, PricingEntry],
) -> tuple[MatcherCandidate, ...]:
    if not normalized_model:
        return ()

    candidates: list[MatcherCandidate] = []
    for entry in index.values():
        norm_key = normalize_model_name(entry.key)
        shorter = norm_key if len(norm_key) < len(normalized_model) else normalized_model
        if len(shorter) < MIN_SUBSTRING_LENGTH:
            continue
        if normalized_model in norm_key or norm_key in normalized_model:
            candidates.append(
                MatcherCandidate(
                    json_key=entry.key,
                    input_cost_per_token=entry.input_cost_per_token,
                    output_cost_per_token=entry.output_cost_per_token,
                    score=0.85,
                    matcher_name="substring",
                )
            )

    return tuple(candidates)


DEFAULT_MATCHERS: tuple[Matcher, ...] = (
    exact_match,
    structured_match,
    fuzzy_match,
    substring_match,
)
