"""Resolve LiteLLM deployments the caller may invoke into discovery targets.

Reuses LiteLLM's existing auth/model-access filtering via
``get_available_models_for_user`` (design §15). A model the caller cannot
invoke must not appear in discovery results.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from litellm.router import Router

from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.common_utils.user_api_key_cache import UserApiKeyCache
from litellm.proxy.openrouter_compat.transport.client import DiscoveryTarget
from litellm.proxy.utils import PrismaClient, ProxyLogging
from litellm.types.router import Deployment


class DeploymentDescriptor(BaseModel):
    deployment_id: str
    logical_model_name: str
    provider: str | None = None
    model: str | None = None
    api_base: str | None = None
    auth_headers: dict[str, str] | None = None
    model_info: dict[str, object] = Field(default_factory=dict)

    def to_discovery_target(self) -> DiscoveryTarget | None:
        if not self.api_base:
            return None
        return DiscoveryTarget(
            deployment_id=self.deployment_id,
            api_base=self.api_base,
            auth_headers=self.auth_headers or {},
        )


class DeploymentResolver:
    """Filter accessible model groups and build discovery targets."""

    def __init__(self, llm_router: Router | None) -> None:
        self._llm_router = llm_router

    async def resolve_for_request(
        self,
        *,
        user_api_key_dict: UserAPIKeyAuth,
        general_settings: dict[str, object],
        prisma_client: PrismaClient | None,
        proxy_logging_obj: ProxyLogging | None,
        user_api_key_cache: UserApiKeyCache | None,
        team_id: str | None = None,
    ) -> list[DeploymentDescriptor]:
        from litellm.proxy.utils import get_available_models_for_user  # pyright: ignore[reportUnknownVariableType]  # litellm.proxy.utils.get_available_models_for_user has unparameterized dict signature

        available: list[str] = await get_available_models_for_user(
            user_api_key_dict=user_api_key_dict,
            llm_router=self._llm_router,
            general_settings=general_settings,
            user_model=None,
            prisma_client=prisma_client,
            proxy_logging_obj=proxy_logging_obj,
            team_id=team_id,
            include_model_access_groups=False,
            only_model_access_groups=False,
            return_wildcard_routes=False,
            user_api_key_cache=user_api_key_cache,
        )
        accessible = set(available)

        descriptors: list[DeploymentDescriptor] = []
        seen_targets: set[tuple[str, str]] = set()
        for deployment in self._iter_deployments():
            logical_name = deployment.model_name
            if logical_name not in accessible:
                continue
            descriptor = self._build_descriptor(deployment, logical_name)
            if descriptor is None:
                continue
            key = (descriptor.api_base or "", descriptor.provider or "")
            if key in seen_targets:
                continue
            seen_targets.add(key)
            descriptors.append(descriptor)
        return descriptors

    def _iter_deployments(self) -> list[Deployment]:
        if self._llm_router is None:
            return []
        model_list = self._llm_router.get_model_list() or []
        deployments: list[Deployment] = []
        for entry in model_list:
            deployments.append(
                Deployment(**entry)  # pyright: ignore[reportArgumentType]  # Deployment.__init__ accepts extra params
            )
        return deployments

    def _build_descriptor(
        self, deployment: Deployment, logical_model_name: str
    ) -> DeploymentDescriptor | None:
        litellm_params = deployment.litellm_params
        api_base = litellm_params.api_base
        if not api_base:
            return None
        auth_headers: dict[str, str] = {}
        api_key = litellm_params.api_key
        if isinstance(api_key, str) and api_key:
            auth_headers["Authorization"] = f"Bearer {api_key}"
        deployment_id = deployment.model_info.id or logical_model_name
        model_info = deployment.model_info.model_dump(exclude_none=True)
        return DeploymentDescriptor(
            deployment_id=deployment_id,
            logical_model_name=logical_model_name,
            provider=litellm_params.custom_llm_provider,
            model=litellm_params.model,
            api_base=api_base,
            auth_headers=auth_headers,
            model_info=model_info,
        )