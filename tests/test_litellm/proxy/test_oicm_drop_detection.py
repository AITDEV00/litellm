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

import pytest

import litellm


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
    assert hasattr(PrometheusLogger, "_reconcile_deployment_in_flight")
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