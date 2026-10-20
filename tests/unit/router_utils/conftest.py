"""Shared fixtures for router_utils tests."""

import pytest

from litellm.caching.dual_cache import DualCache


@pytest.fixture
def dual_cache():
    return DualCache()
