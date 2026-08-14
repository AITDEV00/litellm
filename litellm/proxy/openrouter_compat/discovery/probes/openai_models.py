"""GET /v1/models probe for OpenAI-compatible runtimes."""

from __future__ import annotations

from pydantic import TypeAdapter, ValidationError

from litellm.proxy.openrouter_compat.discovery.probes.base import (
    BaseProbe,
)
from litellm.proxy.openrouter_compat.transport.client import DiscoveryTarget
from litellm.proxy.openrouter_compat.transport.dto import RuntimeModelCard
from litellm.proxy.openrouter_compat.transport.errors import DiscoverySchemaError


class OpenAIModelsProbe(BaseProbe[list[RuntimeModelCard]]):
    name = "openai_models"

    async def _fetch(self, target: DiscoveryTarget) -> list[RuntimeModelCard]:
        payload = await self._client.get_json(target, "/v1/models")
        raw_cards = payload.get("data", [])
        if not isinstance(raw_cards, list):
            raise DiscoverySchemaError("GET /v1/models data is not a list")
        adapter = TypeAdapter(list[RuntimeModelCard])
        try:
            return adapter.validate_python(raw_cards)
        except ValidationError as exc:
            raise DiscoverySchemaError(f"invalid model cards: {exc}") from exc
