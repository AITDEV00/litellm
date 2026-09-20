"""Runtime discovery adapters."""

from litellm.proxy.openrouter_compat.discovery.adapters.openai_compatible import (
    OpenAICompatibleRuntimeAdapter,
)
from litellm.proxy.openrouter_compat.discovery.adapters.sglang import (
    SGLangDiscoveryAdapter,
)
from litellm.proxy.openrouter_compat.discovery.adapters.vllm import VLLMDiscoveryAdapter

__all__ = [
    "OpenAICompatibleRuntimeAdapter",
    "SGLangDiscoveryAdapter",
    "VLLMDiscoveryAdapter",
]