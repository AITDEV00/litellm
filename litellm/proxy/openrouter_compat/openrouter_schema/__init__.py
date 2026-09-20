"""OpenRouter public-contract schema (design §24).

Facade over the official installed ``openrouter`` SDK. Only the mapper layer
imports these types; discovery and the canonical domain never depend on them.
"""

from litellm.proxy.openrouter_compat.openrouter_schema.models import (
    DefaultParameters,
    Model,
    ModelArchitecture,
    ModelLinks,
    Parameter,
    PerRequestLimits,
    PublicPricing,
    TopProviderInfo,
)

__all__ = [
    "DefaultParameters",
    "Model",
    "ModelArchitecture",
    "ModelLinks",
    "Parameter",
    "PerRequestLimits",
    "PublicPricing",
    "TopProviderInfo",
]