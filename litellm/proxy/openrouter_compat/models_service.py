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
from litellm.proxy.openrouter_compat.gateway_status import (
    GatewayStateResolver,
    GatewayStatus,
)
from litellm.proxy.openrouter_compat.mapping.endpoints import (
    OpenRouterEndpointsMapper,
)
from litellm.proxy.openrouter_compat.mapping.openrouter import OpenRouterModelMapper
from litellm.proxy.openrouter_compat.openrouter_schema.models import Model
from litellm.proxy.openrouter_compat.service import DiscoveryService
from litellm.proxy.openrouter_compat.status_reader import GatewayStatusReader
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
        self._endpoints_mapper = OpenRouterEndpointsMapper(pricing_resolver=self._pricing)
        self._gateway_state = GatewayStateResolver()

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
        aggregated, failed = await self._resolve_and_discover(
            user_api_key_dict=user_api_key_dict,
            general_settings=general_settings,
            prisma_client=prisma_client,
            proxy_logging_obj=proxy_logging_obj,
            user_api_key_cache=user_api_key_cache,
            team_id=team_id,
        )
        model = self._find_model(aggregated, author=author, slug=slug)
        public_id = self._mapper.canonical_id(f"{author}/{slug}")
        if model is None:
            # A model the list route knows about but could not discover has no
            # deployments. Answer with an empty endpoint list rather than 404,
            # so the ``links.details`` the list advertises is never dead.
            if self._is_known_undiscovered(failed, public_id=public_id):
                return self._endpoints_mapper.map_empty(
                    public_id=public_id,
                    logical_model_name=slug,
                )
            return None
        page = model.model_copy(update={"deployments": model.deployments[offset : offset + limit]})
        enriched = self._metadata_enricher.enrich(self._capability_enricher.enrich(page))
        statuses = await self._resolve_statuses(enriched, prisma_client)
        return self._endpoints_mapper.map_endpoints(
            enriched,
            public_id=public_id,
            statuses=statuses,
        )

    def _is_known_undiscovered(self, failed: set[str], *, public_id: str) -> bool:
        # ``failed`` holds logical model names, so a bare id appears bare there.
        # Canonicalize both sides or ``/api/v1/models/wrong/hamsa-tts/endpoints``
        # would match on the bare slug and answer for an author it does not
        # belong to.
        return public_id in {self._mapper.canonical_id(name) for name in failed}

    async def _resolve_statuses(
        self,
        model: AggregatedModel,
        prisma_client: PrismaClient | None,
    ) -> dict[str, GatewayStatus]:
        deployment_ids = [deployment.runtime.deployment_id for deployment in model.deployments]
        inputs = await GatewayStatusReader(prisma_client).read(
            model_name=model.logical_model_name,
            deployment_ids=deployment_ids,
        )
        return {
            deployment_id: self._gateway_state.resolve(deployment_inputs)
            for deployment_id, deployment_inputs in inputs.items()
        }

    def _find_model(
        self,
        aggregated: list[AggregatedModel],
        *,
        author: str,
        slug: str,
    ) -> AggregatedModel | None:
        # Match the canonical id for both URL forms. A model id containing a
        # slash (deepseek-ai/DeepSeek-V4) arrives split into author + slug, so
        # its canonical id is "author/slug". A bare id (hamsa-tts) is
        # namespaced by the mapper into "litellm/hamsa-tts", and its URL may
        # carry either that namespaced form or the bare id itself. Reusing the
        # mapper's rule keeps one definition of the namespace.
        requested = self._mapper.canonical_id(f"{author}/{slug}")
        return next(
            (m for m in aggregated if self._mapper.canonical_id(m.logical_model_name) == requested),
            None,
        )

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
