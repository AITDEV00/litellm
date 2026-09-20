"""OpenRouter public-contract types.

Thin facade over the official ``openrouter`` Python SDK (design §24). The SDK is
a pinned runtime dependency; only the mapper layer imports these types.
Discovery and the canonical domain never depend on them.
"""

from openrouter.types.basemodel import (
    UNSET,
    UNSET_SENTINEL,
    Unset,
    UnrecognizedStr,
)
from openrouter.types.basemodel import Nullable, OptionalNullable

__all__ = [
    "UNSET",
    "UNSET_SENTINEL",
    "Unset",
    "UnrecognizedStr",
    "Nullable",
    "OptionalNullable",
]