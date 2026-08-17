import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import httpx

from .config import (
    CONTROLLER_READ_ONLY,
    HTTP_CONCURRENCY,
    LITELLM_ADMIN_KEY,
    LITELLM_ADMIN_URL,
)
from .models import OicmModel, to_litellm_mode

logger = logging.getLogger("oicm-discovery")


class LiteLLMClient:
    def __init__(
        self,
        base_url: str = LITELLM_ADMIN_URL,
        admin_key: str = LITELLM_ADMIN_KEY,
        concurrency: int = HTTP_CONCURRENCY,
        read_only: bool = CONTROLLER_READ_ONLY,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        }
        self._semaphore = asyncio.Semaphore(concurrency)
        self.read_only = read_only

    async def list_all_models_by_key(self) -> dict[str, list[dict]]:
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
                        # A deployment can host multiple models (uuid -> N model
                        # names). Group by composite `{uuid}::{model_name}` so the
                        # reconciler can match each model to its deployment record.
                        model_name = m.get("model_name") or ""
                        key = f"{oicm_uuid}::{model_name}"
                        grouped.setdefault(key, []).append(m)
                return grouped
            except Exception as e:
                logger.error(f"Failed to list models: {e}")
                return {}

    async def batch(
        self,
        deletes: List[str],
        registers: List[Tuple[OicmModel, Optional[dict]]],
        patches: List[Tuple[str, dict, Optional[Dict[str, str]]]],
    ) -> Tuple[int, List[Optional[str]], int]:
        if self.read_only:
            if deletes:
                logger.info(f"[READ-ONLY] would delete: {deletes}")
            for model, _ in registers:
                logger.info(
                    f"[READ-ONLY] would register {model.model_name} "
                    f"(mode={model.mode}, provider={model.provider})"
                )
            for mid, params, _ in patches:
                logger.info(
                    f"[READ-ONLY] would patch {mid}: "
                    f"model={params.get('model')}"
                )
            return 0, [], 0

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
                    self._patch_one(http_client, mid, params, model_info)
                    for mid, params, model_info in patches
                )
            )

            del_results, reg_results, pat_results = await asyncio.gather(
                del_coro, reg_coro, pat_coro
            )
        deleted = sum(1 for r in del_results if r)
        # Keep None placeholders (failed registers) so callers can align results
        # back to the input registers by position. Filtering them out here would
        # break the 1:1 mapping when a mid-batch register fails.
        registered_ids = list(reg_results)
        patched = sum(1 for r in pat_results if r)
        return deleted, registered_ids, patched

    async def register_model(
        self, model: OicmModel, inherited_params: Optional[dict] = None
    ) -> Optional[str]:
        if self.read_only:
            logger.info(
                f"[READ-ONLY] would register {model.model_name} "
                f"(mode={model.mode}, provider={model.provider}, "
                f"api_base={model.api_base})"
            )
            return None
        _, registered_ids, _ = await self.batch([], [(model, inherited_params)], [])
        return next((r for r in registered_ids if r), None)

    async def deregister_model(self, litellm_model_id: str) -> bool:
        if self.read_only:
            logger.info(f"[READ-ONLY] would deregister {litellm_model_id}")
            return False
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
            except Exception as e:
                logger.error(f"Failed to deregister {mid}: {e}")
                return False

    async def _register_one(
        self,
        client: httpx.AsyncClient,
        model: OicmModel,
        inherited_params: Optional[dict] = None,
    ) -> Optional[str]:
        litellm_mode = to_litellm_mode(model.mode)

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
            except Exception as e:
                logger.error(
                    f"Failed to register {model.model_name}: {e}"
                )
                return None

    async def _patch_one(
        self,
        client: httpx.AsyncClient,
        litellm_model_id: str,
        litellm_params: dict,
        model_info: Optional[Dict[str, str]] = None,
    ) -> bool:
        body: dict = {"litellm_params": litellm_params}
        if model_info:
            body["model_info"] = model_info
        async with self._semaphore:
            try:
                resp = await client.patch(
                    f"{self.base_url}/model/{litellm_model_id}/update",
                    headers=self.headers,
                    json=body,
                )
                resp.raise_for_status()
                logger.info(f"Patched model litellm_id={litellm_model_id}")
                return True
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Failed to patch {litellm_model_id}: {e.response.text}"
                )
                return False
            except Exception as e:
                logger.error(
                    f"Failed to patch {litellm_model_id}: {e}"
                )
                return False
