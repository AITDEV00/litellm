"""Pricing enrichment. Prefers deployment model_info, falls back to litellm registry (design §14)."""

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
    """Resolve pricing from deployment model_info first, then litellm's registry.

    Never introduces a second pricing DB: deployment-explicit cost overrides
    (carried through discovery) take precedence, and the litellm cost registry
    is the fallback.
    """

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
            # Prefer explicit per-deployment model_info pricing carried through
            # discovery. These override the registry because they reflect the
            # actual gateway billing config for that deployment (design §14).
            pricing = self._resolve_from_model_info(deployment.model_info)
            if pricing is not None:
                return pricing
            # Fall back to litellm's built-in cost registry keyed on the upstream
            # model id.
            model_name = deployment.identity.upstream_model_id
            if not model_name:
                continue
            try:
                model_info = litellm.get_model_info(model_name)
            except Exception:
                continue
            pricing = self._resolve_from_model_info(model_info)
            if pricing is not None:
                return pricing
        return None

    @staticmethod
    def _resolve_from_model_info(model_info: dict[str, object]) -> Pricing | None:
        in_cost = model_info.get("input_cost_per_token")
        out_cost = model_info.get("output_cost_per_token")
        if not isinstance(in_cost, (int, float)) or not isinstance(
            out_cost, (int, float)
        ):
            return None
        if in_cost < 0 or out_cost < 0:
            return None
        return Pricing(
            prompt=PricingResolver._format_cost(in_cost),
            completion=PricingResolver._format_cost(out_cost),
        )

    def _resolve_unknown(self) -> Pricing | None:
        if self._unknown_policy == "free":
            return Pricing(prompt="0", completion="0")
        return None

    @staticmethod
    def _format_cost(cost_per_token: float) -> str:
        # OpenRouter pricing strings are USD per 1M tokens.
        per_million = cost_per_token * 1_000_000
        return f"{per_million:g}"