"""Transport layer for upstream runtime discovery."""

from litellm.proxy.openrouter_compat.transport.client import (
    DiscoveryHTTPClient,
    DiscoveryTarget,
    fingerprint,
)
from litellm.proxy.openrouter_compat.transport.dto import (
    OpenAICompatibleModelCard,
    RuntimeModelCard,
    SGLangModelInfo,
    UpstreamDTO,
)
from litellm.proxy.openrouter_compat.transport.errors import (
    DiscoveryConnectionError,
    DiscoveryError,
    DiscoveryHTTPError,
    DiscoveryInvalidJSON,
    DiscoverySchemaError,
    DiscoveryTimeout,
    MissingRequiredOpenRouterField,
    UnsupportedRuntime,
)

__all__ = [
    "DiscoveryConnectionError",
    "DiscoveryError",
    "DiscoveryHTTPClient",
    "DiscoveryHTTPError",
    "DiscoveryInvalidJSON",
    "DiscoverySchemaError",
    "DiscoveryTarget",
    "DiscoveryTimeout",
    "MissingRequiredOpenRouterField",
    "OpenAICompatibleModelCard",
    "RuntimeModelCard",
    "SGLangModelInfo",
    "UnsupportedRuntime",
    "UpstreamDTO",
    "fingerprint",
]