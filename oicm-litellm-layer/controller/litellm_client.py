import asyncio
import logging
from types import MappingProxyType
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

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
        source_name: str,
        oicm_block: dict,
    ) -> bool:
        """Write a deployment's routing state, origin, and OICM status at once.

        `blocked` is a top-level column and the rest is a nested `model_info`
        object, and the endpoint accepts both in one body, so a status change
        costs one write and one reload rather than two. `model_info` merges
        shallowly, so the whole `oicm` object is always sent: a partial patch
        would drop the keys it omitted.

        `oicm_cluster` and `oicm_source_name` are written here as well as at
        registration so a row that predates either field gains it on its first
        status write.
        """
        if self.read_only:
            logger.info(
                "[READ-ONLY] would set blocked=%s cluster=%s source=%s oicm=%s on %s",
                blocked,
                cluster,
                source_name,
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
                        "model_info": {
                            "oicm_cluster": cluster,
                            "oicm_source_name": source_name,
                            "oicm": oicm_block,
                        },
                    },
                    timeout=self._write_timeout,
                )
                resp.raise_for_status()
                logger.info(
                    "Patched status on litellm_id=%s: cluster=%s source=%s status=%s serving=%s blocked=%s",
                    litellm_model_id,
                    cluster,
                    source_name,
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

    async def report_status(
        self,
        reports: Sequence[dict],
        *,
        reporter: str = "oicm-controller",
    ) -> bool:
        """Persist observed per-model health into the gateway's health table.

        One POST carries the whole cycle's changed models, and the gateway
        reuses its native health write path, so the rows come back through
        `/health/latest` and the Admin UI health column with no read-side
        work. The write is a plain insert: no model row is touched and no
        router reload runs.
        """
        if self.read_only:
            logger.info(
                "[READ-ONLY] would report %d status rows as %s",
                len(reports),
                reporter,
            )
            return False
        if not reports:
            return True
        async with self._semaphore:
            try:
                resp = await self._client.post(
                    f"{self.base_url}/oicm/v1/status-reports",
                    json={"reporter": reporter, "reports": list(reports)},
                    timeout=self._write_timeout,
                )
                resp.raise_for_status()
                return bool(resp.json().get("saved") == len(reports))
            except Exception as e:
                logger.error(
                    "Failed to report %d status rows: %s",
                    len(reports),
                    _error_detail(e),
                )
                return False

    async def report_heartbeats(
        self,
        clusters: Sequence[str],
        *,
        reporter: str = "oicm-controller",
    ) -> Mapping[str, bool]:
        """Record per-source liveness rows in the gateway's health table.

        One POST per heartbeat tick carries every source, so liveness stays one
        write per source per tick. The server stamps `checked_at`, so the
        timestamp means "when the gateway heard from the controller", which the
        controller itself cannot forge or drift.
        """
        if self.read_only:
            logger.info(
                "[READ-ONLY] would report heartbeats for %s as %s",
                list(clusters),
                reporter,
            )
            return {}
        if not clusters:
            return {}
        async with self._semaphore:
            try:
                resp = await self._client.post(
                    f"{self.base_url}/oicm/v1/heartbeats",
                    json={
                        "reporter": reporter,
                        "heartbeats": [{"cluster": c} for c in clusters],
                    },
                    timeout=self._write_timeout,
                )
                resp.raise_for_status()
                body = resp.json()
            except Exception as e:
                logger.error(
                    "Failed to report heartbeats for %s: %s",
                    list(clusters),
                    _error_detail(e),
                )
                return {}
        saved: dict[str, bool] = {}
        for cluster, ok in zip(clusters, body.get("saved_per_cluster", []), strict=False):
            saved[cluster] = bool(ok)
        if len(saved) != len(clusters):
            # The route saves per source; treat a short answer as all-or-nothing
            # so the caller cannot mistake a partial save for a full one.
            all_ok = bool(body.get("saved") == len(clusters))
            saved = {cluster: all_ok for cluster in clusters}
        return MappingProxyType(saved)

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