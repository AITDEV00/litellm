"""Map one discovered deployment into an OpenRouter endpoint (design §31).

Only this layer imports the OpenRouter schema. The mapper is a pure function of
its inputs: the logical model, its deployments, and the gateway status resolved
for each deployment. Everything the endpoint advertises that we cannot observe
is left null.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import Field

from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.domain.logical_model import AggregatedModel
from litellm.proxy.openrouter_compat.enrichment.pricing import Pricing, PricingResolver
from litellm.proxy.openrouter_compat.enrichment.telemetry import Percentiles, PerDeploymentMetrics
from litellm.proxy.openrouter_compat.gateway_status import GatewayStatus
from litellm.proxy.openrouter_compat.openrouter_schema.base import UnrecognizedStr
from litellm.proxy.openrouter_compat.openrouter_schema.endpoints import (
    Architecture,
    ListEndpointsResponse,
    PercentileStats,
    PublicEndpoint,
    ToolChoiceSupport,
)
from litellm.proxy.openrouter_compat.openrouter_schema.endpoints import (
    Pricing as EndpointPricing,
)

# OpenRouter's own display convention is "<provider>: <model>".
_NAME_SEPARATOR: Final = ": "
_UNPRICED: Final = Pricing(prompt="0", completion="0")


class GatewayEndpoint(PublicEndpoint):
    """An official ``PublicEndpoint`` plus this gateway's serving verdict and live load.

    Extra to the OpenRouter contract on purpose: OpenRouter has no field for
    "can this gateway reach the deployment" (``gateway_status``), nor for the
    live in-flight count (``live_concurrency``) or the 30-minute request volume
    (``requests_last_30m``), which OpenRouter only exposes nested per workload
    inside ``perf_last_30m_by_workload``. ``None`` means the value is not
    observable here (unmanaged deployment, no Prometheus, or no traffic).
    """

    gateway_status: GatewayStatus | None = Field(default=None)
    live_concurrency: int | None = Field(default=None)
    requests_last_30m: float | None = Field(default=None)


class GatewayListEndpointsResponse(ListEndpointsResponse):
    """``ListEndpointsResponse`` whose endpoint list carries the gateway's extra fields.

    Redeclaring ``endpoints`` as the subclass is what keeps ``gateway_status``,
    ``live_concurrency``, and ``requests_last_30m`` in the payload: the base
    field is ``list[PublicEndpoint]``, so assigning ``GatewayEndpoint`` values
    to it would validate each one down to the base type and drop them.
    """

    endpoints: Sequence[GatewayEndpoint]


def _percentile_stats(percentiles: Percentiles | None) -> PercentileStats | None:
    if percentiles is None:
        return None
    return PercentileStats(p50=percentiles.p50, p75=percentiles.p75, p90=percentiles.p90, p99=percentiles.p99)


class OpenRouterEndpointsMapper:
    """Build the ``/endpoints`` payload for one logical model."""

    def __init__(
        self,
        *,
        pricing_resolver: PricingResolver | None = None,
    ) -> None:
        self._pricing_resolver = pricing_resolver or PricingResolver()

    def map_endpoints(
        self,
        model: AggregatedModel,
        *,
        public_id: str,
        statuses: Mapping[str, GatewayStatus],
        metrics: Mapping[str, PerDeploymentMetrics] | None = None,
    ) -> dict[str, object]:
        resolved_metrics: Final = metrics or {}
        response: Final = GatewayListEndpointsResponse(
            id=public_id,
            name=self._display_name(model, public_id),
            created=model.identity.created or 0,
            description=self._description(model, public_id),
            architecture=self._response_architecture(),
            endpoints=tuple(
                self._to_endpoint(
                    model,
                    deployment,
                    statuses.get(deployment.runtime.deployment_id),
                    resolved_metrics.get(deployment.runtime.deployment_id),
                )
                for deployment in model.deployments
            ),
        )
        return {"data": response.model_dump(mode="json")}

    def map_empty(self, *, public_id: str, logical_model_name: str) -> dict[str, object]:
        """A conformant response for a model with no discoverable deployment.

        Keeps the ``links.details`` the list route advertises from being a dead
        URL: the shape is valid, the endpoint list is honestly empty.
        """
        response: Final = GatewayListEndpointsResponse(
            id=public_id,
            name=logical_model_name,
            created=0,
            description=(
                f"{logical_model_name} has no discoverable deployment; "
                "discovery produced no usable endpoint."
            ),
            architecture=self._response_architecture(),
            endpoints=(),
        )
        return {"data": response.model_dump(mode="json")}

    def _to_endpoint(
        self,
        model: AggregatedModel,
        deployment: DiscoveredDeploymentModel,
        status: GatewayStatus | None,
        metrics: PerDeploymentMetrics | None,
    ) -> GatewayEndpoint:
        pricing = self._pricing_resolver.resolve_for_deployments([deployment]) or _UNPRICED
        runtime_kind = deployment.runtime.kind
        model_name = deployment.identity.upstream_model_id or model.logical_model_name
        return GatewayEndpoint(
            context_length=deployment.limits.context_length or 0,
            latency_last_30m=_percentile_stats(metrics.ttft_latency_ms if metrics else None),
            max_completion_tokens=deployment.limits.max_completion_tokens,
            max_prompt_tokens=deployment.limits.max_input_tokens,
            model_id=model.logical_model_name,
            model_name=model_name,
            name=f"{runtime_kind}{_NAME_SEPARATOR}{model_name}",
            native_tools={},
            pricing=EndpointPricing(prompt=pricing.prompt, completion=pricing.completion),
            provider_name=UnrecognizedStr(runtime_kind),
            quantization=None,
            supported_parameters=[],
            supports_implicit_caching=False,
            supports_tool_choice=ToolChoiceSupport(auto=False, function=False, none=False, required=False),
            tag=runtime_kind,
            throughput_last_30m=_percentile_stats(metrics.throughput_tokens_per_sec if metrics else None),
            uptime_last_1d=metrics.uptime_last_1d if metrics else None,
            uptime_last_30m=metrics.uptime_last_30m if metrics else None,
            uptime_last_5m=metrics.uptime_last_5m if metrics else None,
            status=status.endpoint_status() if status else None,
            supports_image_reference=False,
            supports_multiple_audio_references=False,
            supports_voice_cloning=False,
            gateway_status=status,
            live_concurrency=metrics.live_concurrency if metrics else None,
            requests_last_30m=metrics.requests_last_30m if metrics else None,
        )

    @staticmethod
    def _display_name(model: AggregatedModel, public_id: str) -> str:
        return model.identity.display_name or model.identity.upstream_model_id or public_id

    @staticmethod
    def _description(model: AggregatedModel, public_id: str) -> str:
        count = len(model.deployments)
        return f"{public_id} served by the gateway across {count} deployment{'s' if count != 1 else ''}."

    @staticmethod
    def _response_architecture() -> Architecture:
        return Architecture(
            modality=None,
            input_modalities=[],
            output_modalities=[],
            tokenizer=None,
            instruct_type=None,
        )
