import logging
from typing import Optional

from ..config import PRICING_ENABLED
from .aggregator import aggregate
from .matchers import DEFAULT_MATCHERS, Matcher
from .models import PricingResult
from .normalizer import normalize_model_name
from .source import PricingSource

logger = logging.getLogger("oicm-discovery")


class PricingResolver:
    def __init__(
        self,
        source: PricingSource,
        matchers: tuple[Matcher, ...] = DEFAULT_MATCHERS,
    ):
        self._source = source
        self._matchers = matchers
        self._enabled = PRICING_ENABLED

    async def resolve(self, model_id: str) -> Optional[PricingResult]:
        if not self._enabled or not model_id:
            return None

        index = await self._source.get_index()
        if not index:
            logger.warning(
                "Pricing index unavailable; skipping pricing for %s", model_id
            )
            return None

        normalized = normalize_model_name(model_id)

        candidates = []
        for matcher in self._matchers:
            try:
                candidates.extend(matcher(normalized, index.by_normalized_key))
            except Exception as e:
                logger.error(
                    "Matcher %s failed for %s: %s",
                    getattr(matcher, "__name__", matcher),
                    model_id,
                    e,
                )

        result = aggregate(candidates)
        if result:
            logger.info(
                "Pricing resolved for %s: input=%.4e output=%.4e score=%.2f "
                "strategy=%s keys=%s",
                model_id,
                result.input_cost_per_token,
                result.output_cost_per_token,
                result.aggregate_score,
                result.strategy,
                result.matched_keys,
            )
        else:
            logger.info(
                "No pricing match for %s (normalized=%s)", model_id, normalized
            )
        return result
