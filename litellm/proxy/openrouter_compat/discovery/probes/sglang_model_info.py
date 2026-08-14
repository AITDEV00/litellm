"""GET /model_info (fallback /get_model_info) probe for SGLang."""

from __future__ import annotations

from pydantic import TypeAdapter, ValidationError

from litellm.proxy.openrouter_compat.discovery.probes.base import BaseProbe
from litellm.proxy.openrouter_compat.transport.client import DiscoveryTarget
from litellm.proxy.openrouter_compat.transport.dto import SGLangModelInfo
from litellm.proxy.openrouter_compat.transport.errors import (
    DiscoveryHTTPError,
    DiscoverySchemaError,
)


class SGLangModelInfoProbe(BaseProbe[SGLangModelInfo]):
    name = "sglang_model_info"
    _paths = ("/model_info", "/get_model_info")

    async def _fetch(self, target: DiscoveryTarget) -> SGLangModelInfo:
        adapter = TypeAdapter(SGLangModelInfo)
        for path in self._paths:
            try:
                payload = await self._client.get_json(target, path)
            except DiscoveryHTTPError as exc:
                if exc.status_code == 404:
                    continue
                raise
            try:
                return adapter.validate_python(payload)
            except ValidationError as exc:
                raise DiscoverySchemaError(f"{path} invalid: {exc}") from exc
        raise DiscoveryHTTPError(404, "no model_info endpoint available")