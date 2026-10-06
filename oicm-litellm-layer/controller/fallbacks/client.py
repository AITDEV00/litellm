import logging
from typing import Dict, List, Optional

import httpx

from ..config import HTTP_TIMEOUT_SECONDS

logger = logging.getLogger("oicm-discovery")


class FallbackClient:
    def __init__(
        self,
        base_url: str,
        headers: dict,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self._client = http_client or httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_SECONDS, headers=headers
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_model_names(self) -> set[str]:
        try:
            resp = await self._client.get(f"{self.base_url}/model/info")
            resp.raise_for_status()
        except Exception as e:
            logger.error("Failed to list model names: %s", e)
            return set()

        return {
            name
            for m in resp.json().get("data", [])
            if (name := m.get("model_name"))
        }

    async def get_fallbacks(self) -> Dict[str, List[str]]:
        try:
            resp = await self._client.get(f"{self.base_url}/router/settings")
            resp.raise_for_status()
        except Exception as e:
            logger.error("Failed to get router settings: %s", e)
            return {}

        raw_fallbacks = resp.json().get("current_values", {}).get("fallbacks") or []
        return {
            model: targets
            for entry in raw_fallbacks
            if isinstance(entry, dict) and len(entry) == 1
            for model, targets in [next(iter(entry.items()))]
            if isinstance(targets, list)
        }


