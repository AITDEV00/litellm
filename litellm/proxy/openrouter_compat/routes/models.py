"""FastAPI routes for the OpenRouter-compatible model discovery.

Handler-only layer (design §12): authenticate, parse query, call service,
serialize response. No probing or mapping logic lives here.
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from litellm.proxy.auth.user_api_key_auth import (
    UserAPIKeyAuth,
    user_api_key_auth,
)
from litellm.proxy.openrouter_compat.models_service import OpenRouterModelsService

router = APIRouter(tags=["openrouter-compatible"])


def _get_service(request: Request) -> OpenRouterModelsService:
    """Return the lazily-initialised OpenRouter service bound to the app."""
    from litellm.proxy.proxy_server import llm_router  # noqa: PLC0415  # proxy_server imports these routes, so defer

    if llm_router is None:
        raise HTTPException(status_code=500, detail="Router not initialized")

    app_state = getattr(request.app, "state", None)  # pyright: ignore[reportAny]  # FastAPI Request.app is Any
    service = getattr(app_state, "openrouter_service", None)  # pyright: ignore[reportAny]  # dynamic app state
    if service is None:
        base_url = str(request.base_url).rstrip("/")
        service = OpenRouterModelsService(llm_router, details_base_url=base_url)
        if app_state is not None:
            setattr(app_state, "openrouter_service", service)  # pyright: ignore[reportAny]  # dynamic app state
    return cast(OpenRouterModelsService, service)


@router.get("/api/v1/models")
async def list_models(
    request: Request,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
    team_id: str | None = None,
):
    """OpenRouter-compatible model listing (kept separate from litellm /v1/models)."""
    import litellm.proxy.proxy_server as proxy_server_mod  # noqa: PLC0415  # proxy_server imports this router, so lazy-import

    service = _get_service(request)
    settings: dict[str, object] = cast(dict[str, object], proxy_server_mod.general_settings)
    return await service.list_models(
        user_api_key_dict=user_api_key_dict,
        general_settings=settings,
        prisma_client=proxy_server_mod.prisma_client,
        proxy_logging_obj=proxy_server_mod.proxy_logging_obj,
        user_api_key_cache=proxy_server_mod.user_api_key_cache,
        team_id=team_id,
        offset=offset,
        limit=limit,
    )


@router.get("/api/v1/models/{author}/{slug}/endpoints")
async def model_endpoints(
    author: str,
    slug: str,
    request: Request,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
):
    """OpenRouter-compatible per-model endpoint details (design §31)."""
    import litellm.proxy.proxy_server as proxy_server_mod  # noqa: PLC0415  # proxy_server imports this router, so lazy-import

    service = _get_service(request)
    settings: dict[str, object] = cast(dict[str, object], proxy_server_mod.general_settings)

    result = await service.get_model_endpoints(
        author=author,
        slug=slug,
        user_api_key_dict=user_api_key_dict,
        general_settings=settings,
        prisma_client=proxy_server_mod.prisma_client,
        proxy_logging_obj=proxy_server_mod.proxy_logging_obj,
        user_api_key_cache=proxy_server_mod.user_api_key_cache,
        team_id=None,
        offset=offset,
        limit=limit,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return result
