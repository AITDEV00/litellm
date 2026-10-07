import asyncio
import logging
import os
from typing import Dict, Optional

import httpx
from kubernetes import client, config

from ..config import (
    CLUSTER_DOMAIN,
    DISCOVER_CONCURRENCY,
    MODEL_DEPLOYMENT_TYPE,
    MODEL_PORT,
    NAMESPACE,
    PROBE_TIMEOUT_SECONDS,
    WORKLOAD_ID_LABEL,
    WORKLOAD_TYPE_LABEL,
)
from ..models import (
    OicmModel,
    build_model,
    detect_api_surface,
    detect_mode_from_paths,
    detect_provider,
    parse_model_list,
)
from .base import ModelSource

logger = logging.getLogger("oicm-discovery")


def load_kube_config() -> None:
    """Load kube config from KUBECONFIG, in-cluster, or the default path.

    Shared by every source so the three-way fallback lives in one place.
    """
    kubeconfig_path = os.getenv("KUBECONFIG")
    if kubeconfig_path:
        config.load_kube_config(config_file=kubeconfig_path)
        return
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


class LocalDeploymentSource(ModelSource):
    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        try:
            load_kube_config()
        except Exception as e:
            logger.error("Failed to load kube config: %s", e)
            raise

        self.apps_api = client.AppsV1Api()
        self.core_api = client.CoreV1Api()
        self._client = http_client or httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def discover(self) -> Dict[str, OicmModel]:
        loop = asyncio.get_event_loop()
        deployments = await loop.run_in_executor(
            None,
            lambda: self.apps_api.list_namespaced_deployment(
                namespace=NAMESPACE,
                label_selector=f"{WORKLOAD_TYPE_LABEL}={MODEL_DEPLOYMENT_TYPE}",
            ),
        )

        candidates = [
            dep for dep in deployments.items if dep.metadata.labels.get(WORKLOAD_ID_LABEL, "")
        ]
        if not candidates:
            return {}

        # Each deployment costs a ConfigMap read plus two HTTP probes, so the
        # serial fan-out was ~110ms x N. Bounded so a large cluster cannot open
        # an unbounded number of probe sockets at once.
        semaphore = asyncio.Semaphore(DISCOVER_CONCURRENCY)

        async def discover_one(dep) -> Dict[str, OicmModel]:
            async with semaphore:
                return await self.discover_for_deployment(dep)

        results = await asyncio.gather(*(discover_one(dep) for dep in candidates))
        return {uuid: model for result in results for uuid, model in result.items()}

    async def discover_for_deployment(self, dep) -> Dict[str, OicmModel]:
        """Build the OicmModel record for one deployment, keyed by its uuid.

        A deployment serves exactly one model id, so the uuid alone identifies
        it. Keying on uuid rather than a ``{uuid}::{name}`` composite is what
        lets the reconciler match a deployment to its gateway row without
        depending on the model name, which OICM stores as a GUI label.
        """
        uuid = dep.metadata.labels.get(WORKLOAD_ID_LABEL, "")
        ready = dep.status.ready_replicas or 0
        total = dep.status.replicas or 0

        extra_args = await self._get_configmap_field(uuid, "EXTRA_ARGS") or ""
        paths = await self._probe_openapi_paths(uuid)

        model_ids, owned_by = await self._discover_model_ids(uuid)
        if not model_ids:
            model_ids = [uuid]
            logger.warning("Could not discover MODEL_ID for %s, using fallback", uuid)
        elif len(model_ids) > 1:
            logger.warning(
                "Deployment j-%s advertises %d model ids (%s); registering only %s. "
                "One deployment is one model, so the rest are not registered.",
                uuid[:8],
                len(model_ids),
                ", ".join(model_ids),
                model_ids[0],
            )

        # Mode and provider are deployment-level (they depend on the OpenAPI
        # surface, not the individual model id), so compute them once.
        mode = detect_mode_from_paths(paths, model_ids[0], extra_args)
        provider = detect_provider(owned_by or "", model_ids[0], paths)
        api_surface = detect_api_surface(provider, paths)

        model = build_model(
            uuid=uuid,
            model_id=model_ids[0],
            ready_replicas=ready,
            total_replicas=total,
            mode=mode,
            provider=provider,
            extra_args=extra_args,
            api_surface=api_surface,
        )
        return {model.deployment_id: model}

    async def _discover_model_ids(self, uuid: str) -> tuple[list[str], Optional[str]]:
        """Return (model_ids, owned_by) for a deployment.

        Precedence: an explicit ConfigMap MODEL_ID (single, backward compat)
        wins; otherwise the model ids advertised by the backend's `/v1/models`.
        Returns empty list if neither is available.
        """
        cm_model_id = await self._get_configmap_field(uuid, "MODEL_ID")
        if cm_model_id and cm_model_id.strip():
            return [cm_model_id.strip()], None

        try:
            return await self._query_v1_models(uuid)
        except Exception as e:
            logger.debug("Failed to query /v1/models for %s: %s", uuid, e)

        return [], None

    async def _get_configmap_field(self, uuid: str, field: str) -> Optional[str]:
        cm_name = f"configmap-{uuid}-main"
        loop = asyncio.get_event_loop()
        try:
            cm = await loop.run_in_executor(
                None,
                lambda: self.core_api.read_namespaced_config_map(
                    name=cm_name, namespace=NAMESPACE
                ),
            )
            return cm.data.get(field)
        except Exception as e:
            logger.debug("Failed to read ConfigMap %s: %s", cm_name, e)
            return None

    async def _query_v1_models(self, uuid: str) -> tuple[list[str], Optional[str]]:
        url = f"http://s-{uuid}.{NAMESPACE}.{CLUSTER_DOMAIN}:{MODEL_PORT}/v1/models"
        resp = await self._client.get(url)
        if resp.status_code == 405:
            logger.info(
                "Model %s returned 405 on /v1/models, non-OpenAI, skipping", uuid
            )
            return [], None
        resp.raise_for_status()
        data = resp.json()
        model_ids = parse_model_list(data)
        owned_by = None
        openai_data = data.get("data") if isinstance(data, dict) else None
        if isinstance(openai_data, list) and openai_data and isinstance(openai_data[0], dict):
            owned_by = openai_data[0].get("owned_by", "")
        return model_ids, owned_by

    async def _probe_openapi_paths(self, uuid: str) -> frozenset[str]:
        url = f"http://s-{uuid}.{NAMESPACE}.{CLUSTER_DOMAIN}:{MODEL_PORT}/openapi.json"
        try:
            resp = await self._client.get(url)
            if resp.status_code != 200:
                return frozenset()
            return frozenset(resp.json().get("paths", {}).keys())
        except Exception as e:
            logger.debug("Failed to probe /openapi.json for %s: %s", uuid, e)
            return frozenset()
