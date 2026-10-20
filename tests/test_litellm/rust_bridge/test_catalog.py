from __future__ import annotations

from collections.abc import Generator
from typing import Final

import pytest

from litellm.rust_bridge import catalog, configuration
from litellm.rust_bridge.catalog import Delivery, Route, RouteContext, RouteRule
from litellm.rust_bridge.configuration import Decision, Rollout


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    monkeypatch.delenv("LITELLM_RUST", raising=False)
    configuration.reset_rust_configuration()
    yield
    configuration.reset_rust_configuration()


@pytest.mark.parametrize("route", tuple(Route))
@pytest.mark.parametrize("provider", (None, "bedrock", "mistral", "anthropic", "openai", "azure_ai", "unknown"))
@pytest.mark.parametrize("delivery", tuple(Delivery))
@pytest.mark.parametrize("process", (None, False, True))
@pytest.mark.parametrize("environment", (None, "0", "1"))
def test_shipped_decisions(
    monkeypatch: pytest.MonkeyPatch,
    route: Route,
    provider: str | None,
    delivery: Delivery,
    process: bool | None,
    environment: str | None,
) -> None:
    configuration.rust(process)
    if environment is not None:
        monkeypatch.setenv("LITELLM_RUST", environment)
    context: Final = RouteContext(route, provider=provider, model="test-model", delivery=delivery)

    if route is Route.OCR:
        assert catalog.rollout(context) is Rollout.RUST_REQUIRED
        assert catalog.decision(context) is Decision.RUST_REQUIRED
    elif route is Route.TRANSCRIPTION and provider == "bedrock":
        assert catalog.rollout(context) is Rollout.RUST_REQUIRED
        assert catalog.decision(context) is Decision.RUST_REQUIRED
    elif route is Route.MESSAGES and provider == "anthropic":
        assert catalog.rollout(context) is Rollout.RUST_OPT_IN
        opted_in: Final = environment == "1" if environment is not None else process is True
        assert catalog.decision(context) is (Decision.RUST_WITH_FALLBACK if opted_in else Decision.PYTHON)
    else:
        assert catalog.rollout(context) is Rollout.PYTHON_ONLY
        assert catalog.decision(context) is Decision.PYTHON


@pytest.mark.parametrize("route", tuple(Route))
def test_missing_rule_stays_on_python_even_when_rust_is_enabled(monkeypatch: pytest.MonkeyPatch, route: Route) -> None:
    configuration.rust(True)
    monkeypatch.setenv("LITELLM_RUST", "1")

    assert catalog.rollout(RouteContext(route), rules=()) is Rollout.PYTHON_ONLY
    assert catalog.decision(RouteContext(route), rules=()) is Decision.PYTHON


@pytest.mark.parametrize(
    ("context", "expected"),
    (
        (RouteContext(Route.RESPONSES, provider="openai", model="m", delivery=Delivery.WEBSOCKET), Decision.RUST_REQUIRED),
        (RouteContext(Route.RESPONSES, provider="openai", model="m"), Decision.PYTHON),
        (RouteContext(Route.RESPONSES, provider="openai", model="m", delivery=Delivery.STREAMING), Decision.PYTHON),
        (RouteContext(Route.RESPONSES, provider="openai", model="other", delivery=Delivery.WEBSOCKET), Decision.PYTHON),
        (RouteContext(Route.RESPONSES, provider="anthropic", model="m", delivery=Delivery.WEBSOCKET), Decision.PYTHON),
        (RouteContext(Route.MESSAGES, provider="openai", model="m", delivery=Delivery.WEBSOCKET), Decision.PYTHON),
    ),
)
def test_first_matching_rule_respects_every_constraint(context: Context, expected: Decision) -> None:
    rules: Final = (
        RouteRule(
            Route.RESPONSES,
            Rollout.RUST_REQUIRED,
            providers=frozenset({"openai"}),
            models=frozenset({"m"}),
            deliveries=frozenset({Delivery.WEBSOCKET}),
        ),
        RouteRule(Route.RESPONSES, Rollout.PYTHON_ONLY),
    )

    assert catalog.decision(context, rules) is expected


@pytest.mark.parametrize("provider", ("paddlex", "mistral", "aws_textract", "azure_ai", "cohere", "reducto"))
def test_every_ocr_provider_routes_to_rust_because_python_ocr_no_longer_exists(provider: str) -> None:
    """Upstream removed the Python OCR execution path, so a provider pinned to
    Python would have nothing to dispatch to. Every OCR provider, including the
    OICM-custom paddlex, must reach the Rust route."""
    configuration.rust(True)

    context: Final = RouteContext(Route.OCR, provider=provider, model="PP-DocLayoutV3")

    assert catalog.rollout(context) is Rollout.RUST_REQUIRED
    assert catalog.decision(context) is Decision.RUST_REQUIRED


@pytest.mark.parametrize("process", (None, False, True))
@pytest.mark.parametrize("environment", (None, "0", "1"))
def test_textract_ocr_has_no_python_path_to_opt_out_to(
    monkeypatch: pytest.MonkeyPatch, process: bool | None, environment: str | None
) -> None:
    configuration.rust(process)
    if environment is not None:
        monkeypatch.setenv("LITELLM_RUST", environment)

    assert catalog.decision(RouteContext(Route.OCR, provider="aws_textract", model="m")) is Decision.RUST_REQUIRED
