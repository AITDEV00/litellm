"""OpenRouter-compatible model discovery service (design §41)."""

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.openrouter_compat.aggregation.aggregator import ModelAggregator
from litellm.proxy.openrouter_compat.cache.memory import InMemoryDiscoveryCache
from litellm.proxy.openrouter_compat.discovery.registry import DiscoveryAdapterRegistry
from litellm.proxy.openrouter_compat.discovery.resolver import (
    DeploymentDescriptor,
    DeploymentResolver,
)
from litellm.proxy.openrouter_compat.domain.logical_model import AggregatedModel
from litellm.proxy.openrouter_compat.enrichment.capabilities import CapabilityEnricher
from litellm.proxy.openrouter_compat.enrichment.litellm_metadata import (
    LiteLLMMetadataEnricher,
)
from litellm.proxy.openrouter_compat.enrichment.pricing import PricingResolver
from litellm.proxy.openrouter_compat.mapping.openrouter import OpenRouterModelMapper
from litellm.proxy.openrouter_compat.openrouter_schema.models import Model
from litellm.proxy.openrouter_compat.service import DiscoveryService
from litellm.proxy.openrouter_compat.transport.client import DiscoveryHTTPClient
from litellm.proxy.utils import PrismaClient, ProxyLogging
from litellm.router import Router


class OpenRouterModelsService:
    """Orchestrates resolve -> discover -> aggregate -> enrich -> map."""

    def __init__(
        self,
        llm_router: Router | None,
        *,
        details_base_url: str,
        http_client: DiscoveryHTTPClient | None = None,
        cache: InMemoryDiscoveryCache | None = None,
        is_moderated_default: bool = False,
    ) -> None:
        self._resolver = DeploymentResolver(llm_router)
        self._http_client = http_client or DiscoveryHTTPClient()
        self._registry = DiscoveryAdapterRegistry(self._http_client)
        self._cache = cache or InMemoryDiscoveryCache()
        self._discovery = DiscoveryService(self._registry, self._cache)
        self._aggregator = ModelAggregator()
        self._capability_enricher = CapabilityEnricher()
        self._metadata_enricher = LiteLLMMetadataEnricher()
        self._pricing = PricingResolver()
        self._mapper = OpenRouterModelMapper(
            details_base_url=details_base_url,
            is_moderated_default=is_moderated_default,
            pricing_resolver=self._pricing,
        )

    async def list_models(
        self,
        *,
        user_api_key_dict: UserAPIKeyAuth,
        general_settings: dict[str, object],
        prisma_client: PrismaClient | None,
        proxy_logging_obj: ProxyLogging | None,
        user_api_key_cache: UserApiKeyCache | None,
        team_id: str | None = None,
        offset: int = 0,
        limit: int = 500,
    ) -> dict[str, object]:
        aggregated, failed = await self._resolve_and_discover(
            user_api_key_dict=user_api_key_dict,
            general_settings=general_settings,
            prisma_client=prisma_client,
            proxy_logging_obj=proxy_logging_obj,
            user_api_key_cache=user_api_key_cache,
            team_id=team_id,
        )
        enriched = [self._metadata_enricher.enrich(self._capability_enricher.enrich(m)) for m in aggregated]
        output: list[Model] = [self._mapper.map_model(m) for m in enriched]
        output.extend(self._mapper.map_placeholder(logical_model_name) for logical_model_name in sorted(failed))
        total_count = len(output)
        page = output[offset : offset + limit]
        return {
            "data": page,
            "total_count": total_count,
            "links": {"next": self._build_next_link(offset, limit, total_count)},
        }

    async def get_model_endpoints(
        self,
        *,
        author: str,
        slug: str,
        user_api_key_dict: UserAPIKeyAuth,
        general_settings: dict[str, object],
        prisma_client: PrismaClient | None,
        proxy_logging_obj: ProxyLogging | None,
        user_api_key_cache: UserApiKeyCache | None,
        team_id: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, object] | None:
        aggregated, _failed = await self._resolve_and_discover(
            user_api_key_dict=user_api_key_dict,
            general_settings=general_settings,
            prisma_client=prisma_client,
            proxy_logging_obj=proxy_logging_obj,
            user_api_key_cache=user_api_key_cache,
            team_id=team_id,
        )
        full_slug = f"{author}/{slug}"
        for model in aggregated:
            name = model.identity.logical_model_name
            # Match the canonical slug ("author/slug") for ids that contain a
            # slash, or the bare slug for ids that are namespaced by the mapper
            # (e.g. litellm/hamsa-tts -> logical name "hamsa-tts").
            if name in (full_slug, slug):
                deployments = model.deployments
                page = deployments[offset : offset + limit]
                return {
                    "id": f"{author}/{slug}",
                    "total_count": len(deployments),
                    "data": [
                        {
                            "provider": d.runtime.kind,
                            "context_length": d.limits.context_length,
                            "max_input_tokens": d.limits.max_input_tokens,
                            "max_completion_tokens": d.limits.max_completion_tokens,
                            "capabilities": d.capabilities.model_dump(exclude_none=True),
                            "api_capabilities": d.api_capabilities.model_dump(exclude_none=True),
                        }
                        for d in page
                    ],
                }
        return None

    async def _resolve_and_discover(
        self,
        *,
        user_api_key_dict: UserAPIKeyAuth,
        general_settings: dict[str, object],
        prisma_client: PrismaClient | None,
        proxy_logging_obj: ProxyLogging | None,
        user_api_key_cache: UserApiKeyCache | None,
        team_id: str | None,
    ) -> tuple[list[AggregatedModel], set[str]]:
        deployments: list[DeploymentDescriptor] = await self._resolver.resolve_for_request(
            user_api_key_dict=user_api_key_dict,
            general_settings=general_settings,
            prisma_client=prisma_client,
            proxy_logging_obj=proxy_logging_obj,
            user_api_key_cache=user_api_key_cache,
            team_id=team_id,
        )
        result = await self._discovery.discover_many(deployments)
        aggregated = self._aggregator.aggregate_all(result.discoveries)
        return aggregated, result.failed_logical_models

    @staticmethod
    def _build_next_link(offset: int, limit: int, total_count: int) -> str | None:
        next_offset = offset + limit
        if next_offset >= total_count:
            return None
        return f"/api/v1/models?offset={next_offset}&limit={limit}"

    async def aclose(self) -> None:
        await self._http_client.aclose()
