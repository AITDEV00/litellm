import logging
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("oicm-discovery")


class FallbackClient:
    def __init__(self, base_url: str, headers: dict):
        self.base_url = base_url.rstrip("/")
        self.headers = headers

    async def list_model_names(self) -> set[str]:
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            try:
                resp = await http_client.get(
                    f"{self.base_url}/model/info",
                    headers=self.headers,
                )
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Failed to list model names: {e}")
                return set()

        names: set[str] = set()
        for m in resp.json().get("data", []):
            name = m.get("model_name")
            if name:
                names.add(name)
        return names

    async def get_fallbacks(self) -> Dict[str, List[str]]:
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            try:
                resp = await http_client.get(
                    f"{self.base_url}/router/settings",
                    headers=self.headers,
                )
                resp.raise_for_status()
            except Exception as e:
                logger.error(f"Failed to get router settings: {e}")
                return {}

        current_values = resp.json().get("current_values", {})
        raw_fallbacks = current_values.get("fallbacks") or []

        result: Dict[str, List[str]] = {}
        for entry in raw_fallbacks:
            if isinstance(entry, dict) and len(entry) == 1:
                model = list(entry.keys())[0]
                targets = list(entry.values())[0]
                if isinstance(targets, list):
                    result[model] = targets
        return result

    async def set_fallback(self, model: str, fallback_models: List[str]) -> bool:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            try:
                resp = await http_client.post(
                    f"{self.base_url}/fallback",
                    headers=self.headers,
                    json={
                        "model": model,
                        "fallback_models": fallback_models,
                        "fallback_type": "general",
                    },
                )
                resp.raise_for_status()
                logger.info(f"Set fallback: {model} -> {fallback_models}")
                return True
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Failed to set fallback for {model}: {e.response.text}"
                )
                return False
