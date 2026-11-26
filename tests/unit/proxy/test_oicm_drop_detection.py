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
import inspect
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


def test_drama3_provider_dispatch_wired() -> None:
    """The drama3 provider slice must stay dispatched through utils after a merge."""
    from litellm.utils import ProviderConfigManager

    voice = ProviderConfigManager.get_provider_voice_config(litellm.LlmProviders.DRAMA3)
    assert voice is not None, "DRAMA3 voice config dispatch was dropped"
    assert voice.__class__.__name__ == "Drama3VoiceConfig"

    tts = ProviderConfigManager.get_provider_text_to_speech_config(
        model="drama3/fishaudio/drama-3",
        provider=litellm.LlmProviders.DRAMA3,
    )
    assert tts is not None, "DRAMA3 TTS config dispatch was dropped"
    assert tts.__class__.__name__ == "Drama3TextToSpeechConfig"


def test_drama3_speech_dispatch_tail_wired() -> None:
    """speech() must keep dispatching drama3 through the provider-config branch.

    The tail's elif tuple is inline in litellm/main.py, so a merge that drops
    'drama3' from it routes requests to the OpenAI branch and silently strips
    references again. Assert the source text rather than behavior: this is a
    drop-detection test, not a behavioral one.
    """
    from pathlib import Path

    main_py = Path(litellm.__file__).parent / "main.py"
    source = main_py.read_text()
    assert '"drama3"' in source, "drama3 was dropped from the speech() dispatch tail"
    assert (
        '"omnivoice",\n        "drama3",' in source
        or '"hamsa",\n        "inception",\n        "omnivoice",\n        "drama3",' in source
    )


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

    mounted_paths = {route.path for route in app.routes if getattr(route, "path", None) and "/install" in route.path}
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

    mounted_paths = {route.path for route in app.routes if getattr(route, "path", None) and "/diarize" in route.path}
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
    "litellm.proxy.audio_translation_routes",
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


def test_hamsa_api_surface_reaches_provider_params() -> None:
    from litellm.litellm_core_utils.get_litellm_params import get_litellm_params

    params = get_litellm_params(model="hamsa/default", api_base="https://tts.test", api_surface="v1")
    assert params["api_surface"] == "v1"


# ---------------------------------------------------------------------------
# Slice: audio translation route + SDK (litellm/proxy/audio_translation_routes.py,
# litellm/endpoints/audio_translation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/audio/translations", "/v1/audio/translations"],
)
def test_audio_translation_routes_router_is_mounted_on_app(path: str) -> None:
    """Both alias paths must be included on the FastAPI app after a merge."""
    from litellm.proxy.proxy_server import app

    assert any(getattr(route, "path", None) == path for route in app.routes), (
        f"{path} is not mounted; audio_translation_routes slice was dropped"
    )


def test_audio_translation_routes_module_has_router() -> None:
    from litellm.proxy.audio_translation_routes import router

    assert len(router.routes) > 0, "audio_translation_routes router is empty"


@pytest.mark.parametrize(
    "name",
    ["translation", "atranslation"],
)
def test_audio_translation_sdk_slice_wired_into_litellm_namespace(name: str) -> None:
    fn = getattr(litellm, name)
    assert callable(fn)
    assert fn.__module__.startswith("litellm.endpoints.audio_translation"), (
        f"litellm.{name} no longer resolves to the audio_translation slice"
    )


def test_audio_translation_provider_registry_dispatch_wired() -> None:
    """The provider config manager must dispatch translation for NIM + hosted vLLM."""
    from litellm.types.utils import LlmProviders
    from litellm.utils import ProviderConfigManager

    for provider in (LlmProviders.NVIDIA_NIM, LlmProviders.HOSTED_VLLM):
        cfg = ProviderConfigManager.get_provider_audio_translation_config(model="probe", provider=provider)
        assert cfg is not None, f"{provider.value} audio translation dispatch was dropped"


def test_audio_translation_route_type_dispatch_wired() -> None:
    """route_llm_request must map atranslation to the /audio/translations endpoint."""
    from litellm.proxy.route_llm_request import ROUTE_ENDPOINT_MAPPING

    assert ROUTE_ENDPOINT_MAPPING.get("atranslation") == "/audio/translations", (
        "atranslation dispatch mapping was dropped from route_llm_request"
    )


def test_audio_translation_resolves_guardrail_call_types() -> None:
    from litellm.litellm_core_utils.api_route_to_call_types import get_call_types_for_route

    assert get_call_types_for_route("/v1/audio/translations"), (
        "/v1/audio/translations resolves no CallTypes, so unified guardrails skip it"
    )


def test_async_inference_slices_are_not_classified_as_sync() -> None:
    from litellm.litellm_core_utils.litellm_logging import Logging

    assert Logging.is_sync_litellm_request({"acreate_voice": True}) is False
    assert Logging.is_sync_litellm_request({"adiarize": True}) is False
    assert Logging.is_sync_litellm_request({"atranslation": True}) is False


def test_openrouter_compatible_catalog_is_mounted_on_app() -> None:
    from litellm.proxy.proxy_server import app

    assert any(route.path == "/api/v1/models" for route in app.routes)


@pytest.mark.parametrize("path", ["/model/performance", "/model/metrics/per_model"])
def test_model_performance_routes_are_mounted_on_app(path: str) -> None:
    from litellm.proxy.proxy_server import app

    assert any(route.path == path for route in app.routes)


# ---------------------------------------------------------------------------
# Slice: prisma-engine memory guards (upstream-owned files carrying OICM edits)
# ---------------------------------------------------------------------------
# These pin behavioral bounds that live INSIDE upstream-owned files, not wiring
# between slices, so the mount/re-export checks above cannot see them. The
# v1.108.5 merge took the upstream blob wholesale for these files and dropped
# every one of these bounds together with their per-feature tests. A symbol
# pin here survives that class of resolution because this file is OICM-owned:
# a wholesale takeover of an upstream file cannot delete this test, only fail
# it. The behavioral contracts stay in each feature's own test module.


def test_end_user_alias_scan_cap_present() -> None:
    """The unfiltered customer daily-activity alias pre-fetch must stay capped.

    Without the cap it reads the whole LiteLLM_EndUserTable per call and the
    engine's high-water mark ratchets (the 2026-11 prod OOM driver)."""
    from litellm.proxy.management_endpoints.customer_endpoints import (
        _END_USER_ALIAS_SCAN_CAP,
        resolve_customer_daily_activity_scope,
    )

    assert _END_USER_ALIAS_SCAN_CAP > 0
    src: Final = inspect.getsource(resolve_customer_daily_activity_scope)
    assert "take=_END_USER_ALIAS_SCAN_CAP" in src, "alias pre-fetch lost its take cap"


def test_all_end_users_facet_scan_is_bounded() -> None:
    """The /global/all_end_users DISTINCT must scan a bounded recent subquery.

    An unbounded SELECT DISTINCT over the spend log ships the whole deduplicated
    set through the engine in one statement."""
    from litellm.proxy.spend_tracking import spend_management_endpoints as sme

    assert sme.END_USER_FACET_SCAN_CAP > 0
    src: Final = inspect.getsource(sme.global_view_all_end_users)
    assert "LIMIT $1" in src, "all_end_users facet lost its bounded subquery"


def test_global_spend_refresh_disconnects_client() -> None:
    """The /global/spend/refresh dedicated client must disconnect in a finally.

    Each PrismaClient owns a query-engine subprocess; a never-disconnected one
    strands a full engine per call for the worker's lifetime."""
    from litellm.proxy.spend_tracking import spend_management_endpoints as sme

    src: Final = inspect.getsource(sme.global_spend_refresh)
    assert "finally:" in src and "disconnect()" in src, "refresh client lost its disconnect"


def test_model_performance_rollup_read_sums_histograms_in_sql() -> None:
    """The rollup read must fold per-minute histograms in SQL, not json_agg.

    json_agg made each result row carry one histogram array per minute in its
    bucket; a full-range payload hit ~62MB and deserialized to a 4.5GB engine
    high-water mark. The element-wise SUM keeps one fixed-size array per row."""
    from litellm.proxy.model_metrics_endpoints import model_performance_endpoints as mpe

    src: Final = inspect.getsource(mpe._fetch_db_performance_from_rollup)
    sql: Final = src.split('sql_query = f"""', 1)[1]
    assert "_rollup_array_sum(ttft_histogram_counts)" in sql, "rollup read lost its in-SQL histogram sum"
    assert "json_agg(ttft_histogram_counts)" not in sql, "rollup read reverted to json_agg histogram collection"


def test_daily_activity_grouping_caps_rows_per_level() -> None:
    """The GROUPING SETS rollup must rank and cap rows per group level.

    Without the cap the multi-level GROUPING SETS query ships every rollup
    level's full rowset in one statement."""
    from litellm.constants import MAX_ROWS_PER_GROUPING_LEVEL
    from litellm.repositories import daily_activity_sql
    from litellm.types.repositories.daily_activity import DailyActivityScope, DailyActivityTable

    assert MAX_ROWS_PER_GROUPING_LEVEL > 0
    src: Final = inspect.getsource(daily_activity_sql.build_aggregated_sql)
    assert "ROW_NUMBER()" in src and "level_rank <=" in src, "grouping-sets read lost its per-level cap"
    scope: Final = DailyActivityScope(
        table=DailyActivityTable.CUSTOMER,
        entity_id_field="end_user_id",
        entity_ids=None,
        exclude_entity_ids=(),
        api_keys=None,
        start_date="2026-01-01",
        end_date="2026-01-02",
        model=None,
        timezone_offset_minutes=None,
    )
    params: Final = daily_activity_sql.build_aggregated_sql(scope, api_key_limit=5).params
    assert MAX_ROWS_PER_GROUPING_LEVEL in params, "per-level cap param missing from the statement"


def test_heavy_query_client_is_per_call_not_cached() -> None:
    """The >=14d model-performance client must connect/disconnect per call.

    A cached heavy client strands its engine (multi-GB high-water mark) per
    worker for the worker's lifetime; two such engines OOMKilled prod pods. The
    client must be an async context manager entered with ``async with`` at both
    heavy call sites, never awaited as a bare singleton getter."""
    from litellm.proxy.model_metrics_endpoints import model_performance_endpoints as mpe

    src: Final = inspect.getsource(mpe._heavy_query_prisma_client)
    assert "finally:" in src and "disconnect()" in src, "heavy client lost its disconnect"
    for fn in (mpe._fetch_db_performance, mpe._fetch_db_performance_from_rollup):
        call_src: Final = inspect.getsource(fn)
        assert "async with _heavy_query_prisma_client()" in call_src, (
            f"{fn.__name__} no longer uses the per-call heavy client context"
        )


def test_stream_tracer_records_chunk_and_terminal_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    from litellm.litellm_core_utils import stream_tracer

    monkeypatch.setattr(stream_tracer, "_ENABLED", True)
    events: list[object] = []
    monkeypatch.setattr(stream_tracer, "_emit_fn", events.append)
    tracer = stream_tracer.begin_trace(request_id="request", model="model", provider="provider", api_base="base")
    assert tracer is not None
    tracer.record_chunk(chars=4, finish_reason="stop")
    tracer.record_end("success")
    import json

    records = tuple(json.loads(event) for event in events)
    assert tuple((event["event"], event["chunk_seq"], event["end_kind"]) for event in records) == (
        ("start", None, None),
        ("first_chunk", 1, None),
        ("end", 1, "success"),
    )
    assert records[1]["chunk_chars"] == 4
    assert records[1]["finish_reason"] == "stop"
