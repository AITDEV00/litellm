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
from .models import OicmModel, strip_source_prefix, to_litellm_mode

logger = logging.getLogger("oicm-discovery")


def gateway_uuid(oicm_uuid: str) -> str:
    """The bare deployment uuid behind a possibly source-prefixed gateway uuid.

    ``submariner:abudhabi:<uuid>`` becomes ``<uuid>``; anything else is returned
    unchanged, so a local uuid and an unrecognized value both pass through.
    """
    return strip_source_prefix(oicm_uuid)


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
            # One deployment is one model, so the uuid alone is the identity.
            # Keying on it rather than on `{uuid}::{model_name}` is what makes
            # the join work for a deployment whose OICM GUI label differs from
            # the id its server serves: the name here is the one the controller
            # itself registered, so it already agrees with discovery.
            #
            # A Submariner import stores `submariner:<cluster>:<uuid>`, which no
            # OICM reports, so the prefix is stripped to reach the bare uuid its
            # own source returns. Without this a cross-cluster row can never
            # find its status.
            grouped.setdefault(gateway_uuid(oicm_uuid), []).append(m)
        return grouped

    async def patch_status(
        self,
        litellm_model_id: str,
        blocked: bool,
        cluster: str,
        oicm_block: dict,
    ) -> bool:
        """Write a deployment's routing state, cluster, and OICM status at once.

        `blocked` is a top-level column and the rest is a nested `model_info`
        object, and the endpoint accepts both in one body, so a status change
        costs one write and one reload rather than two. `model_info` merges
        shallowly, so the whole `oicm` object is always sent: a partial patch
        would drop the keys it omitted.

        `oicm_cluster` is written here as well as at registration so a row that
        predates it gains the field on its first status write.
        """
        if self.read_only:
            logger.info(
                "[READ-ONLY] would set blocked=%s cluster=%s oicm=%s on %s",
                blocked,
                cluster,
                oicm_block.get("status"),
                litellm_model_id,
            )
            return False
        async with self._semaphore:
            try:
                resp = await self._client.patch(
                    f"{self.base_url}/model/{litellm_model_id}/update",
                    json={
                        "blocked": blocked,
                        "model_info": {"oicm_cluster": cluster, "oicm": oicm_block},
                    },
                    timeout=self._write_timeout,
                )
                resp.raise_for_status()
                logger.info(
                    "Patched status on litellm_id=%s: cluster=%s status=%s serving=%s blocked=%s",
                    litellm_model_id,
                    cluster,
                    oicm_block.get("status"),
                    oicm_block.get("serving_available"),
                    blocked,
                )
                return True
            except Exception as e:
                logger.error(
                    "Failed to patch status on %s: %s",
                    litellm_model_id,
                    _error_detail(e),
                )
                return False

    async def upsert_heartbeat(
        self, row: dict, existing_id: Optional[str] = None
    ) -> bool:
        """Create or advance one controller-owned liveness row per source.

        Liveness is a per-source fact, so this is one write per source per
        heartbeat rather than one per model. The row carries no `oicm_uuid`,
        which is what keeps it invisible to ``list_all_models_by_key`` and to
        every reconciliation rule, and it is blocked so it is never routable.

        `existing_id` makes this an upsert: without it, every heartbeat would
        POST a new row and the gateway would accumulate one per tick.
        """
        if self.read_only:
            logger.info("[READ-ONLY] would upsert heartbeat %s", row["model_name"])
            return False
        if existing_id is not None:
            return await self._patch_heartbeat(existing_id, row)
        async with self._semaphore:
            try:
                resp = await self._client.post(
                    f"{self.base_url}/model/new",
                    json=row,
                    timeout=self._write_timeout,
                )
                resp.raise_for_status()
                created_id = resp.json().get("model_id")
                if created_id:
                    await self.set_blocked(created_id, True)
                return True
            except Exception as e:
                logger.error(
                    "Failed to create heartbeat %s: %s",
                    row["model_name"],
                    _error_detail(e),
                )
                return False

    async def _patch_heartbeat(self, litellm_model_id: str, row: dict) -> bool:
        async with self._semaphore:
            try:
                resp = await self._client.patch(
                    f"{self.base_url}/model/{litellm_model_id}/update",
                    json={
                        "blocked": True,
                        "model_info": row["model_info"],
                    },
                    timeout=self._write_timeout,
                )
                resp.raise_for_status()
                return True
            except Exception as e:
                logger.error(
                    "Failed to advance heartbeat %s: %s",
                    row["model_name"],
                    _error_detail(e),
                )
                return False

    async def list_heartbeats(self) -> Dict[str, str]:
        """Map heartbeat name to its LiteLLM model id, one row per source.

        Read from the same `/model/info` payload the reconciler already uses, so
        the heartbeat costs no extra call. Rows are matched on
        `model_info.oicm_heartbeat`, the tag that distinguishes them from real
        deployments.
        """
        try:
            resp = await self._client.get(f"{self.base_url}/model/info")
            resp.raise_for_status()
        except Exception as e:
            logger.error("Failed to list heartbeats: %s", e)
            return {}
        found: Dict[str, str] = {}
        for m in resp.json().get("data", []):
            info = m.get("model_info") or {}
            name = info.get("oicm_heartbeat")
            model_id = info.get("id")
            if name and model_id:
                found[name] = model_id
        return found

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

    async def set_blocked(self, litellm_model_id: str, blocked: bool) -> bool:
        """Pause or resume a registered model without touching its config.

        `blocked` is a top-level column, so it cannot ride along in a
        litellm_params patch. This is what makes a non-serving deployment stay
        registered and visible while being excluded from routing.
        """
        if self.read_only:
            logger.info(
                "[READ-ONLY] would set blocked=%s on %s", blocked, litellm_model_id
            )
            return False
        async with self._semaphore:
            try:
                resp = await self._client.patch(
                    f"{self.base_url}/model/{litellm_model_id}/update",
                    json={"blocked": blocked},
                    timeout=self._write_timeout,
                )
                resp.raise_for_status()
                logger.info(
                    "Set blocked=%s on litellm_id=%s", blocked, litellm_model_id
                )
                return True
            except Exception as e:
                logger.error(
                    "Failed to set blocked=%s on %s: %s",
                    blocked,
                    litellm_model_id,
                    _error_detail(e),
                )
                return False

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


def heartbeat_payload(name: str, checked_at: str) -> dict:
    """The request body for one source's liveness row.

    The `api_base` is an unroutable placeholder and the model is a passthrough
    id, because the row exists only to carry a timestamp. It is created blocked,
    so it is never selected, and having no `oicm_uuid` keeps it out of every
    rule that reconciles real deployments.
    """
    return {
        "model_name": name,
        "litellm_params": {
            "model": "hosted_vllm/__oicm_heartbeat__",
            "api_base": "http://127.0.0.1:1/v1",
            "api_key": "",
        },
        "model_info": {
            "mode": "chat",
            "oicm_heartbeat": name,
            "checked_at": checked_at,
        },
    }


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
            "oicm_cluster": model.cluster,
            "oicm_namespace": model.namespace,
            "oicm_source": model.source,
        },
    }
