import asyncio
import logging
from typing import Dict, List, Optional, Tuple

import httpx

from .config import (
    CONTROLLER_READ_ONLY,
    HTTP_CONCURRENCY,
    HTTP_TIMEOUT_SECONDS,
    LITELLM_ADMIN_KEY,
    LITELLM_ADMIN_URL,
    REMOTE_TIMEOUT_SECONDS,
)
from .models import OicmModel, to_litellm_mode

logger = logging.getLogger("oicm-discovery")


def _admin_headers(admin_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {admin_key}",
        "Content-Type": "application/json",
    }


class LiteLLMClient:
    def __init__(
        self,
        base_url: str = LITELLM_ADMIN_URL,
        admin_key: str = LITELLM_ADMIN_KEY,
        concurrency: int = HTTP_CONCURRENCY,
        read_only: bool = CONTROLLER_READ_ONLY,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = _admin_headers(admin_key)
        self.read_only = read_only
        self._semaphore = asyncio.Semaphore(concurrency)
        self._client = client or httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_SECONDS, headers=self.headers
        )
        # Mutating calls get a tighter timeout than reads; a hung write stalls
        # the reconcile, a hung read only delays it.
        self._write_timeout = httpx.Timeout(REMOTE_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_all_models_by_key(self) -> dict[str, list[dict]]:
        try:
            resp = await self._client.get(f"{self.base_url}/model/info")
            resp.raise_for_status()
        except Exception as e:
            logger.error("Failed to list models: %s", e)
            return {}

        grouped: dict[str, list[dict]] = {}
        for m in resp.json().get("data", []):
            info = m.get("model_info", {})
            oicm_uuid = info.get("oicm_uuid")
            if not oicm_uuid:
                continue
            m["model_id"] = info.get("id")
            # A deployment can host multiple models (uuid -> N model names).
            # Group by composite `{uuid}::{model_name}` so the reconciler can
            # match each model to its deployment record.
            model_name = m.get("model_name") or ""
            grouped.setdefault(f"{oicm_uuid}::{model_name}", []).append(m)
        return grouped

    async def batch(
        self,
        deletes: List[str],
        registers: List[Tuple[OicmModel, Optional[dict]]],
        patches: List[Tuple[str, dict, Optional[Dict[str, str]]]],
    ) -> Tuple[int, List[Optional[str]], int]:
        if self.read_only:
            self._log_read_only_plan(deletes, registers, patches)
            return 0, [], 0

        valid_deletes = [mid for mid in deletes if mid]
        del_results, reg_results, pat_results = await asyncio.gather(
            asyncio.gather(*(self._delete_one(mid) for mid in valid_deletes)),
            asyncio.gather(
                *(self._register_one(model, params) for model, params in registers)
            ),
            asyncio.gather(
                *(self._patch_one(mid, params, info) for mid, params, info in patches)
            ),
        )
        deleted = sum(1 for r in del_results if r)
        # Keep None placeholders (failed registers) so callers can align results
        # back to the input registers by position. Filtering them out here would
        # break the 1:1 mapping when a mid-batch register fails.
        registered_ids = list(reg_results)
        patched = sum(1 for r in pat_results if r)
        return deleted, registered_ids, patched

    def _log_read_only_plan(self, deletes, registers, patches) -> None:
        if deletes:
            logger.info("[READ-ONLY] would delete: %s", deletes)
        for model, _ in registers:
            logger.info(
                "[READ-ONLY] would register %s (mode=%s, provider=%s)",
                model.model_name,
                model.mode,
                model.provider,
            )
        for mid, params, _ in patches:
            logger.info("[READ-ONLY] would patch %s: model=%s", mid, params.get("model"))

    async def register_model(
        self, model: OicmModel, inherited_params: Optional[dict] = None
    ) -> Optional[str]:
        if self.read_only:
            logger.info(
                "[READ-ONLY] would register %s (mode=%s, provider=%s, api_base=%s)",
                model.model_name,
                model.mode,
                model.provider,
                model.api_base,
            )
            return None
        _, registered_ids, _ = await self.batch([], [(model, inherited_params)], [])
        return next((r for r in registered_ids if r), None)

    async def deregister_model(self, litellm_model_id: str) -> bool:
        if self.read_only:
            logger.info("[READ-ONLY] would deregister %s", litellm_model_id)
            return False
        deleted, _, _ = await self.batch([litellm_model_id], [], [])
        return deleted > 0

    async def _delete_one(self, mid: str) -> bool:
        async with self._semaphore:
            try:
                resp = await self._client.post(
                    f"{self.base_url}/model/delete",
                    json={"id": mid},
                    timeout=self._write_timeout,
                )
                resp.raise_for_status()
                return True
            except Exception as e:
                logger.error("Failed to deregister %s: %s", mid, _error_detail(e))
                return False

    async def _register_one(
        self,
        model: OicmModel,
        inherited_params: Optional[dict] = None,
    ) -> Optional[str]:
        payload = _register_payload(model, inherited_params)
        async with self._semaphore:
            try:
                resp = await self._client.post(
                    f"{self.base_url}/model/new",
                    json=payload,
                    timeout=self._write_timeout,
                )
                resp.raise_for_status()
                model_id = resp.json().get("model_id")
                logger.info(
                    "Registered %s (uuid=%s) -> litellm_id=%s",
                    model.model_name,
                    model.uuid[:8],
                    model_id,
                )
                return model_id
            except Exception as e:
                logger.error(
                    "Failed to register %s: %s", model.model_name, _error_detail(e)
                )
                return None

    async def _patch_one(
        self,
        litellm_model_id: str,
        litellm_params: dict,
        model_info: Optional[Dict[str, str]] = None,
    ) -> bool:
        body: dict = {"litellm_params": litellm_params}
        if model_info:
            body["model_info"] = model_info
        async with self._semaphore:
            try:
                resp = await self._client.patch(
                    f"{self.base_url}/model/{litellm_model_id}/update",
                    json=body,
                    timeout=self._write_timeout,
                )
                resp.raise_for_status()
                logger.info("Patched model litellm_id=%s", litellm_model_id)
                return True
            except Exception as e:
                logger.error(
                    "Failed to patch %s: %s", litellm_model_id, _error_detail(e)
                )
                return False


def _error_detail(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.text
    return str(exc)


def _register_payload(model: OicmModel, inherited_params: Optional[dict]) -> dict:
    litellm_params: dict = {
        "model": f"{model.provider}/{model.model_id}",
        "api_base": model.api_base,
        "api_key": "",
        "drop_params": True,
    }
    if model.api_surface:
        # Hamsa pods ship two API generations (native /tts/stream vs v1
        # /v1/speech). The gateway config classes read this param to pick
        # request paths.
        litellm_params["api_surface"] = model.api_surface
    if inherited_params:
        for k, v in inherited_params.items():
            if k not in litellm_params and v is not None:
                litellm_params[k] = v
    return {
        "model_name": model.model_name,
        "litellm_params": litellm_params,
        "model_info": {
            "mode": to_litellm_mode(model.mode),
            "oicm_uuid": model.uuid,
            "oicm_namespace": model.namespace,
            "oicm_source": model.source,
        },
    }
