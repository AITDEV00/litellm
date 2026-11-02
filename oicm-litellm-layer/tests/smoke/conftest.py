"""Shared fixtures for the smoke suite.

Hard failures only: a missing gateway or a missing required key is a failing
error, never a skip. The master key defaults to the deploy manifest; the
scoped key must be exported as ``KEY`` for Tier B and the scoped-auth tests.
"""

from __future__ import annotations

from collections.abc import Generator

import pytest

from .client import Gateway, Resources, build_gateway


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "contract: Tier A, no inference")
    config.addinivalue_line("markers", "model_contract: Tier B, costs tokens")
    config.addinivalue_line("markers", "soak: Tier C, slow leak census")
    config.addinivalue_line("markers", "mutating: changes live gateway state; never in a routine run")


@pytest.fixture(scope="session")
def gw() -> Gateway:
    gateway = build_gateway()
    if gateway.master_key is None:
        pytest.fail(
            "no master key: export MASTER_KEY/LITELLM_MASTER_KEY or ensure "
            "deploy/base/gateway/secrets/litellm-master-key.yaml is present"
        )
    return gateway


@pytest.fixture
def resources(gw: Gateway) -> Generator[Resources]:
    manager = Resources()
    try:
        yield manager
    finally:
        manager.teardown()
