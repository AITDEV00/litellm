"""Drop-detection regression tests for OICM-custom features.

Every OICM feature has been extracted out of upstream-managed files into
co-located slices. An upstream merge can silently drop the *wiring* between
those slices and the public surface (a ``git merge`` that deletes a
co-located module, a mount line, a callback registration, or a re-export)
without failing loudly. These tests pin each wiring point so a dropped
feature fails a test instead of failing at runtime in production.

They are intentionally shallow (assert the wiring exists / resolves) rather
than full behavioral tests: the behavioral contract of each slice is covered
by its own co-located test module. Here we only guarantee the slice is
wired into the runtime.
"""

import importlib
from typing import TYPE_CHECKING, Final

import pytest
from fastapi import HTTPException, Request

import litellm

if TYPE_CHECKING:
    from litellm.proxy._types import UserAPIKeyAuth

# ---------------------------------------------------------------------------
# Slice: voice routes (litellm/proxy/voice_routes.py)
# ---------------------------------------------------------------------------


def test_voice_routes_router_is_mounted_on_app() -> None:
    """The voice router must be included on the FastAPI app after a merge."""
    from litellm.proxy.proxy_server import app

    mounted_router_paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", None) and ("/v1/audio/voices" in route.path or "/voice" in route.path)
    }
    assert mounted_router_paths, "no voice/audio route mounted; voice_routes slice was dropped"


def test_voice_routes_module_has_router() -> None:
    from litellm.proxy.voice_routes import router

    assert len(router.routes) > 0, "voice_routes router is empty"


# ---------------------------------------------------------------------------
# Slice: voice/script SDK (litellm/endpoints/voice)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["create_voice", "acreate_voice", "script", "ascript"],
)
def test_voice_sdk_slice_wired_into_litellm_namespace(name: str) -> None:
    fn = getattr(litellm, name)
    assert callable(fn)
    assert fn.__module__.startswith("litellm.endpoints.voice")


# ---------------------------------------------------------------------------
# Slice: OICM provider dispatch (litellm/llms/oicm_providers)
# ---------------------------------------------------------------------------


def test_oicm_provider_registry_dispatch_wired() -> None:
    """The OICM provider registry must be reachable and dispatch custom providers."""
    from litellm.integrations.prometheus import PrometheusLogger  # noqa: F401  # ensure proxy imports chain

    registry = importlib.import_module("litellm.llms.oicm_providers.registry")
    assert callable(registry.get_provider_voice_config)

    hamsa = registry.get_provider_voice_config(litellm.LlmProviders.HAMSA)
    assert hamsa is not None, "HAMSA voice config dispatch was dropped"
    assert hamsa.__class__.__name__ == "HamsaVoiceConfig"


def test_oicm_provider_dispatch_reachable_through_utils() -> None:
    """utils.py ProviderConfigManager must still delegate to the registry."""
    from litellm.utils import ProviderConfigManager

    voice = ProviderConfigManager.get_provider_voice_config(litellm.LlmProviders.OMNIVOICE)
    assert voice is not None, "OMNIVOICE voice config dispatch was dropped"
    assert voice.__class__.__name__ == "OmniVoiceVoiceConfig"


# ---------------------------------------------------------------------------
# Slice: Prometheus in-flight deployment gauge
# ---------------------------------------------------------------------------


def test_prometheus_in_flight_ledger_wired() -> None:
    """The in-flight ledger must live in the OICM helper slice and be wired into the logger."""
    from litellm.integrations.prometheus import PrometheusLogger
    from litellm.integrations.prometheus_helpers.deployment_in_flight import (
        DeploymentInFlightLedger,
        DeploymentInFlightMetricsMixin,
    )

    assert issubclass(PrometheusLogger, DeploymentInFlightMetricsMixin), "PrometheusLogger lost the in-flight mixin"
    assert hasattr(PrometheusLogger, "async_pre_call_deployment_hook")
    assert hasattr(PrometheusLogger, "_admit_deployment_in_flight")
    assert hasattr(PrometheusLogger, "_release_deployment_in_flight")
    assert hasattr(PrometheusLogger, "evict_stale_deployment_in_flight")
    # The ledger class lives in the slice, not grafted into prometheus.py.
    assert DeploymentInFlightLedger is not None


# ---------------------------------------------------------------------------
# Slice: HTB rate limiter registration
# ---------------------------------------------------------------------------


def test_htb_rate_limiter_callback_registered() -> None:
    """The HTB callback name must stay registered so configs referencing it load."""
    from litellm.proxy.hooks import PROXY_HOOKS

    assert "dynamic_rate_limiter_v3_htb" in PROXY_HOOKS, "HTB rate limiter callback registration was dropped"


def test_htb_rate_limiter_module_present() -> None:
    mod = importlib.import_module("litellm.proxy.hooks.dynamic_rate_limiter_v3_htb")
    assert hasattr(mod, "_PROXY_DynamicRateLimitHandlerV3Htb")


# ---------------------------------------------------------------------------
# Team cache invalidation
# ---------------------------------------------------------------------------


def test_team_cache_invalidation_slice_wired() -> None:
    from litellm.proxy.management_endpoints.team_endpoints import (
        _invalidate_team_key_caches,
    )
    from litellm.proxy.management_helpers.team_cache_invalidation import (
        _invalidate_team_key_caches as slice_fn,
    )

    # team_endpoints must re-import from the slice (not define it inline).
    assert _invalidate_team_key_caches is slice_fn


def test_team_cache_invalidation_wired_into_model_management() -> None:
    from litellm.proxy.management_endpoints.model_management_endpoints import (
        _invalidate_team_key_caches,
    )
    from litellm.proxy.management_helpers.team_cache_invalidation import (
        _invalidate_team_key_caches as slice_fn,
    )

    assert _invalidate_team_key_caches is slice_fn


# ---------------------------------------------------------------------------
# Slice: client distribution (litellm/proxy/client_distribution)
# ---------------------------------------------------------------------------


def test_client_distribution_router_mounted_on_app() -> None:
    """The /install router must be included on the FastAPI app after a merge."""
    from litellm.proxy.proxy_server import app

    mounted_paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", None) and "/install" in route.path
    }
    assert mounted_paths, "no /install route mounted; client_distribution slice was dropped"


def test_client_distribution_module_has_router() -> None:
    from litellm.proxy.client_distribution import router

    assert len(router.routes) > 0, "client_distribution router is empty"


# ---------------------------------------------------------------------------
# Slice: diarization route (litellm/proxy/diarization_routes.py)
# ---------------------------------------------------------------------------


def test_diarization_routes_router_is_mounted_on_app() -> None:
    """The /v1/diarize router must be included on the FastAPI app after a merge."""
    from litellm.proxy.proxy_server import app

    mounted_paths = {
        route.path
        for route in app.routes
        if getattr(route, "path", None) and "/diarize" in route.path
    }
    assert mounted_paths, "no /diarize route mounted; diarization_routes slice was dropped"


def test_diarization_routes_module_has_router() -> None:
    from litellm.proxy.diarization_routes import router

    assert len(router.routes) > 0, "diarization_routes router is empty"


# ---------------------------------------------------------------------------
# Slice: diarization SDK (litellm/endpoints/diarization)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["diarize", "adiarize"],
)
def test_diarization_sdk_slice_wired_into_litellm_namespace(name: str) -> None:
    fn = getattr(litellm, name)
    assert callable(fn)
    assert fn.__module__.startswith("litellm.endpoints.diarization")


def test_diarization_provider_registry_dispatch_wired() -> None:
    """The registry must dispatch pyannote to the diarization config."""
    from litellm.utils import ProviderConfigManager

    cfg = ProviderConfigManager.get_provider_diarization_config(litellm.LlmProviders.PYANNOTE)
    assert cfg is not None, "PYANNOTE diarization config dispatch was dropped"
    assert cfg.__class__.__name__ == "PyannoteDiarizationConfig"


def test_diarization_route_type_dispatch_wired() -> None:
    """route_llm_request must map adiarize to the /v1/diarize endpoint."""
    from litellm.proxy.route_llm_request import ROUTE_ENDPOINT_MAPPING

    assert ROUTE_ENDPOINT_MAPPING.get("adiarize") == "/v1/diarize", (
        "adiarize dispatch mapping was dropped from route_llm_request"
    )


# ---------------------------------------------------------------------------
# Structural guard: every fork-owned inference route must be authorized for
# llm_api_routes keys
# ---------------------------------------------------------------------------

_FORK_INFERENCE_ROUTERS: Final = (
    "litellm.proxy.diarization_routes",
    "litellm.proxy.voice_routes",
)


def test_fork_inference_routes_are_reachable_by_llm_api_keys() -> None:
    """Mounting a fork inference router is not enough: every path it serves
    must also be authorized for keys scoped to allowed_routes=["llm_api_routes"]
    (the preset every key_type=llm_api key gets). The llm_api_routes snapshot
    is built once at LiteLLMRoutes class-definition time, so a route registered
    on the app but absent from the groups is 403 for every llm_api key, while
    the master key (allowed_routes=None) still succeeds and masks the bug in
    gateway-level smoke tests.

    Each router declares an OPS_ONLY_ROUTES set for paths that are pod
    diagnostics rather than inference. Everything else must be reachable, so a
    newly added route has to be classified explicitly instead of silently
    defaulting to denied.

    This is the missing inverse of upstream's test_routes_on_litellm_proxy
    (which checks every *allowlisted* route exists on the app) and of the
    mount tests above (which only check the router is included). It fails for
    /v1/diarize from 2026-10-19 until the group-membership fix.
    """
    from unittest.mock import MagicMock

    from litellm.proxy._types import UserAPIKeyAuth

    served: set[str] = set()
    ops_only: set[str] = set()
    for module_path in _FORK_INFERENCE_ROUTERS:
        module = importlib.import_module(module_path)
        served |= {getattr(route, "path", "") for route in getattr(module.router, "routes", [])}
        ops_only |= set(module.OPS_ONLY_ROUTES)

    assert served, "no fork inference routes found; the routers failed to import"

    unclassified = sorted(ops_only - served)
    assert not unclassified, (
        f"OPS_ONLY_ROUTES names route(s) {unclassified} that the router does not serve. "
        "A renamed or removed path leaves a stale exemption that silently widens the "
        "inference allowlist."
    )

    token = UserAPIKeyAuth(user_id="guard", allowed_routes=["llm_api_routes"])
    request = MagicMock(spec=Request)
    request.method = "POST"

    denied = sorted(path for path in served - ops_only if not _llm_api_key_can_call(path, token, request))

    assert not denied, (
        f"Fork inference route(s) {denied} are mounted but NOT reachable by "
        "llm_api_routes keys (key_type=llm_api). Add them to a group included "
        "in the llm_api_routes snapshot, e.g. litellm_native_routes in "
        "litellm/proxy/_types.py, or list them in the router's OPS_ONLY_ROUTES "
        "if they are diagnostics rather than inference. The snapshot is built "
        "at class-definition time, so a runtime append elsewhere will not help."
    )

    exposed_ops = sorted(path for path in ops_only if _llm_api_key_can_call(path, token, request))
    assert not exposed_ops, (
        f"Route(s) {exposed_ops} are declared OPS_ONLY_ROUTES but are still reachable by "
        "llm_api_routes keys, so the exemption is not actually keeping them off the "
        "inference surface."
    )


def test_fork_inference_routes_resolve_a_guardrail_call_type() -> None:
    """Reachable is not enough: a route that invokes a model must also resolve to
    a CallTypes entry.

    Unified guardrails call ``get_call_types_for_route(user_api_key_dict.request_route)``
    and, on a miss, log a warning and skip post-call scanning. An inference route
    that is authorized but absent from ``API_ROUTE_TO_CALL_TYPES`` therefore
    silently opts out of every guardrail, which is invisible in a 200 response.

    Discovery routes are exempt: upstream's own ``/v1/models`` is in
    llm_api_routes with no CallTypes entry, because listing models invokes no
    model and so has nothing to scan.
    """
    from litellm.litellm_core_utils.api_route_to_call_types import get_call_types_for_route

    missing: list[str] = []
    for module_path in _FORK_INFERENCE_ROUTERS:
        module = importlib.import_module(module_path)
        exempt = set(module.OPS_ONLY_ROUTES) | set(module.DISCOVERY_ROUTES)
        for route in getattr(module.router, "routes", []):
            path = getattr(route, "path", "")
            if not path or path in exempt:
                continue
            concrete = path.replace("{profile_id}", "probe").replace("{model_id}", "probe")
            if not get_call_types_for_route(concrete):
                missing.append(path)

    assert not missing, (
        f"Fork inference route(s) {missing} resolve no CallTypes, so unified guardrails "
        "skip post-call scanning for them with only a log warning. Add them to "
        "API_ROUTE_TO_CALL_TYPES in litellm/types/utils.py using the call type their "
        "handler actually dispatches, or declare them DISCOVERY_ROUTES if they invoke "
        "no model."
    )


def _llm_api_key_can_call(path: str, token: "UserAPIKeyAuth", request: Request) -> bool:
    """Whether an llm_api_routes key may call `path`.

    Only the expected 403 is treated as a denial. Any other exception is a bug in
    the check itself and must surface, otherwise a typo here would read as
    "correctly denied" and silently satisfy the OPS_ONLY assertion.
    """
    from litellm.proxy.auth.route_checks import RouteChecks

    try:
        RouteChecks.is_virtual_key_allowed_to_call_route(route=path, valid_token=token, request=request)
        return True
    except HTTPException as e:
        assert e.status_code == 403, f"{path} rejected with {e.status_code}, expected 403"
        return False

