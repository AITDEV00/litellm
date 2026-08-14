"""Pricing enrichment. Reuses litellm's existing pricing registry (design §14)."""

from __future__ import annotations

from dataclasses import dataclass

import litellm

from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.domain.logical_model import AggregatedModel


@dataclass(frozen=True, slots=True)
class Pricing:
    prompt: str
    completion: str


class PricingResolver:
    """Resolve pricing from litellm's cost registry, never a second pricing DB."""

    def __init__(
        self,
        *,
        unknown_policy: str = "free",
    ) -> None:
        # unknown_policy: "free" reports 0 (no charge); "omit" leaves pricing unset.
        self._unknown_policy = unknown_policy

    def resolve(self, logical_model: AggregatedModel) -> Pricing | None:
        resolved = self._resolve_deployment(logical_model.deployments)
        if resolved is not None:
            return resolved
        return self._resolve_unknown()

    def _resolve_deployment(
        self, deployments: list[DiscoveredDeploymentModel]
    ) -> Pricing | None:
        for deployment in deployments:
            # Prefer explicit deployment model_info pricing already carried in the
            # discovery object. We do not have a direct pricing field, so resolve
            # via litellm registry keyed on the upstream model id.
            model_name = deployment.identity.upstream_model_id
            if not model_name:
                continue
            try:
                model_info = litellm.get_model_info(model_name)
            except Exception:
                continue
            in_cost = model_info.get("input_cost_per_token")
            out_cost = model_info.get("output_cost_per_token")
            if in_cost is None or out_cost is None:
                continue
            return Pricing(
                prompt=self._format_cost(in_cost),
                completion=self._format_cost(out_cost),
            )
        return None

    def _resolve_unknown(self) -> Pricing | None:
        if self._unknown_policy == "free":
            return Pricing(prompt="0", completion="0")
        return None

    @staticmethod
    def _format_cost(cost_per_token: float) -> str:
        # OpenRouter pricing strings are USD per 1M tokens.
        per_million = cost_per_token * 1_000_000
        return f"{per_million:g}"