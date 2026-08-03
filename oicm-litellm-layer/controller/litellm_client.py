import asyncio
import logging
from typing import List, Optional, Tuple

import httpx

from .config import HTTP_CONCURRENCY, LITELLM_ADMIN_KEY, LITELLM_ADMIN_URL
from .models import OicmModel

logger = logging.getLogger("oicm-discovery")


class LiteLLMClient:
    def __init__(
        self,
        base_url: str = LITELLM_ADMIN_URL,
        admin_key: str = LITELLM_ADMIN_KEY,
        concurrency: int = HTTP_CONCURRENCY,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        }
        self._semaphore = asyncio.Semaphore(concurrency)

    async def list_all_models_by_uuid(self) -> dict[str, list[dict]]:
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            try:
                resp = await http_client.get(
                    f"{self.base_url}/model/info",
                    headers=self.headers,
                )
                resp.raise_for_status()
                result = resp.json()
                grouped: dict = {}
                for m in result.get("data", []):
                    info = m.get("model_info", {})
                    oicm_uuid = info.get("oicm_uuid")
                    if oicm_uuid:
                        m["model_id"] = info.get("id")
                        grouped.setdefault(oicm_uuid, []).append(m)
                return grouped
            except Exception as e:
                logger.error(f"Failed to list models: {e}")
                return {}

    async def batch(
        self,
        deletes: List[str],
        registers: List[Tuple[OicmModel, Optional[dict]]],
        patches: List[Tuple[str, dict]],
    ) -> Tuple[int, List[Optional[str]], int]:
        valid_deletes = [mid for mid in deletes if mid]
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            del_coro = asyncio.gather(
                *(self._delete_one(http_client, mid) for mid in valid_deletes)
            )
            reg_coro = asyncio.gather(
                *(
                    self._register_one(http_client, model, params)
                    for model, params in registers
                )
            )
            pat_coro = asyncio.gather(
                *(
                    self._patch_one(http_client, mid, params)
                    for mid, params in patches
                )
            )

            del_results, reg_results, pat_results = await asyncio.gather(
                del_coro, reg_coro, pat_coro
            )
        deleted = sum(1 for r in del_results if r)
        registered_ids = [r for r in reg_results if r]
        patched = sum(1 for r in pat_results if r)
        return deleted, registered_ids, patched

    async def register_model(
        self, model: OicmModel, inherited_params: Optional[dict] = None
    ) -> Optional[str]:
        _, registered_ids, _ = await self.batch([], [(model, inherited_params)], [])
        return registered_ids[0] if registered_ids else None

    async def deregister_model(self, litellm_model_id: str) -> bool:
        deleted, _, _ = await self.batch([litellm_model_id], [], [])
        return deleted > 0

    async def _delete_one(
        self, client: httpx.AsyncClient, mid: str
    ) -> bool:
        async with self._semaphore:
            try:
                resp = await client.post(
                    f"{self.base_url}/model/delete",
                    headers=self.headers,
                    json={"id": mid},
                )
                resp.raise_for_status()
                return True
            except httpx.HTTPStatusError as e:
                logger.error(f"Failed to deregister {mid}: {e.response.text}")
                return False

    async def _register_one(
        self,
        client: httpx.AsyncClient,
        model: OicmModel,
        inherited_params: Optional[dict] = None,
    ) -> Optional[str]:
        litellm_mode = model.mode
        if litellm_mode == "text_to_speech":
            litellm_mode = "chat"

        litellm_params = {
            "model": f"{model.provider}/{model.model_id}",
            "api_base": model.api_base,
            "api_key": "",
            "drop_params": True,
        }

        if inherited_params:
            for k, v in inherited_params.items():
                if k not in litellm_params and v is not None:
                    litellm_params[k] = v

        payload = {
            "model_name": model.model_name,
            "litellm_params": litellm_params,
            "model_info": {
                "mode": litellm_mode,
                "oicm_uuid": model.uuid,
                "oicm_namespace": model.namespace,
                "oicm_source": model.source,
            },
        }

        async with self._semaphore:
            try:
                resp = await client.post(
                    f"{self.base_url}/model/new",
                    headers=self.headers,
                    json=payload,
                )
                resp.raise_for_status()
                result = resp.json()
                model_id = result.get("model_id")
                logger.info(
                    f"Registered {model.model_name} (uuid={model.uuid[:8]}) "
                    f"-> litellm_id={model_id}"
                )
                return model_id
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Failed to register {model.model_name}: {e.response.text}"
                )
                return None

    async def _patch_one(
        self,
        client: httpx.AsyncClient,
        litellm_model_id: str,
        litellm_params: dict,
    ) -> bool:
        async with self._semaphore:
            try:
                resp = await client.patch(
                    f"{self.base_url}/model/{litellm_model_id}/update",
                    headers=self.headers,
                    json={"litellm_params": litellm_params},
                )
                resp.raise_for_status()
                logger.info(f"Patched model litellm_id={litellm_model_id}")
                return True
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Failed to patch {litellm_model_id}: {e.response.text}"
                )
                return False
