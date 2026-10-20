"""Tests for the in-memory discovery cache (design §34).

Regression focus: cache keys never contain plaintext secrets (api_base and
auth identity are fingerprinted), TTL expiry, and LRU eviction.
"""

from __future__ import annotations

from unittest.mock import patch

from litellm.proxy.openrouter_compat.cache.memory import InMemoryDiscoveryCache
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel


def test_cache_key_fingerprints_secrets():
    cache = InMemoryDiscoveryCache()
    key_a = cache.key("dep-1", "vllm", "https://api.example.com", "sk-secret-1")
    key_b = cache.key("dep-1", "vllm", "https://api.example.com", "sk-secret-2")
    assert "sk-secret-1" not in key_a
    assert "sk-secret-2" not in key_b
    assert key_a != key_b


def test_cache_set_get_roundtrip():
    cache = InMemoryDiscoveryCache(ttl_seconds=60)
    value = [
        DiscoveredDeploymentModel.model_validate(
            {
                "identity": {"logical_model_name": "gpt-x"},
                "limits": {},
                "architecture": {},
                "capabilities": {},
                "api_capabilities": {},
                "runtime": {"kind": "vllm", "deployment_id": "dep-1"},
                "provenance": {},
            }
        )
    ]
    cache.set("dep-1", "vllm", "https://api.example.com", "auth-id", value)
    got = cache.get("dep-1", "vllm", "https://api.example.com", "auth-id")
    assert got == value


def test_cache_expires_after_ttl():
    cache = InMemoryDiscoveryCache(ttl_seconds=15)
    cache.set("dep-1", "vllm", "https://api.example.com", "auth-id", [])
    with patch("litellm.proxy.openrouter_compat.cache.memory.time.monotonic", return_value=100.0):
        cache.set("dep-1", "vllm", "https://api.example.com", "auth-id", [])
    # Force expiry by moving clock past TTL.
    with patch("litellm.proxy.openrouter_compat.cache.memory.time.monotonic", return_value=200.0):
        got = cache.get("dep-1", "vllm", "https://api.example.com", "auth-id")
    assert got is None


def test_cache_evicts_lru():
    cache = InMemoryDiscoveryCache(ttl_seconds=60, max_entries=2)
    for i in range(3):
        cache.set(f"dep-{i}", "vllm", f"https://api-{i}.com", "auth-id", [])
    # dep-0 was evicted (LRU), dep-1 and dep-2 remain.
    assert cache.get("dep-0", "vllm", "https://api-0.com", "auth-id") is None
    assert cache.get("dep-1", "vllm", "https://api-1.com", "auth-id") is not None
    assert cache.get("dep-2", "vllm", "https://api-2.com", "auth-id") is not None