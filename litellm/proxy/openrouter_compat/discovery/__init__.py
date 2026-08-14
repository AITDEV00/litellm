"""Discovery subpackage: adapters, probes, registry, resolver."""

from litellm.proxy.openrouter_compat.discovery.adapters import (
    OpenAICompatibleRuntimeAdapter,
    SGLangDiscoveryAdapter,
    VLLMDiscoveryAdapter,
)
from litellm.proxy.openrouter_compat.discovery.registry import DiscoveryAdapterRegistry
from litellm.proxy.openrouter_compat.discovery.resolver import (
    DeploymentDescriptor,
    DeploymentResolver,
)

__all__ = [
    "OpenAICompatibleRuntimeAdapter",
    "SGLangDiscoveryAdapter",
    "VLLMDiscoveryAdapter",
    "DiscoveryAdapterRegistry",
    "DeploymentDescriptor",
    "DeploymentResolver",
]