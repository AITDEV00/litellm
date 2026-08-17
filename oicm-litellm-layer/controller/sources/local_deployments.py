import asyncio
import logging
import os
from typing import Dict, Optional

import httpx
from kubernetes import client, config

from ..config import (
    CLUSTER_DOMAIN,
    MODEL_DEPLOYMENT_TYPE,
    MODEL_PORT,
    NAMESPACE,
    WORKLOAD_ID_LABEL,
    WORKLOAD_TYPE_LABEL,
)
from ..models import (
    OicmModel,
    detect_mode_from_paths,
    detect_provider,
    parse_model_list,
    sanitize_model_id,
)
from .base import ModelSource

logger = logging.getLogger("oicm-discovery")


class LocalDeploymentSource(ModelSource):
    def __init__(self):
        kubeconfig_path = os.getenv("KUBECONFIG")
        try:
            if kubeconfig_path:
                config.load_kube_config(config_file=kubeconfig_path)
            else:
                try:
                    config.load_incluster_config()
                except config.ConfigException:
                    config.load_kube_config()
        except Exception as e:
            logger.error(f"Failed to load kube config: {e}")
            raise

        self.apps_api = client.AppsV1Api()
        self.core_api = client.CoreV1Api()

    async def discover(self) -> Dict[str, OicmModel]:
        loop = asyncio.get_event_loop()
        deployments = await loop.run_in_executor(
            None,
            lambda: self.apps_api.list_namespaced_deployment(
                namespace=NAMESPACE,
                label_selector=f"{WORKLOAD_TYPE_LABEL}={MODEL_DEPLOYMENT_TYPE}",
            ),
        )

        models: Dict[str, OicmModel] = {}
        for dep in deployments.items:
            uuid = dep.metadata.labels.get(WORKLOAD_ID_LABEL, "")
            if not uuid:
                continue
            models.update(await self.discover_for_deployment(dep))
        return models

    async def discover_for_deployment(self, dep) -> Dict[str, OicmModel]:
        """Build the (possibly multiple) OicmModel records for one deployment.

        A deployment can host several models behind the same ClusterIP service
        (e.g. a Triton-style /v1/models advertising multiple ids). Each model id
        becomes its own record, keyed by composite `{uuid}::{model_name}`.
        """
        uuid = dep.metadata.labels.get(WORKLOAD_ID_LABEL, "")
        ready = dep.status.ready_replicas or 0
        total = dep.status.replicas or 0

        extra_args = await self._get_configmap_field(uuid, "EXTRA_ARGS") or ""
        paths = await self._probe_openapi_paths(uuid)

        model_ids, owned_by = await self._discover_model_ids(uuid)
        if not model_ids:
            model_ids = [uuid]
            logger.warning(
                f"Could not discover MODEL_ID for {uuid}, using fallback"
            )

        # Mode and provider are deployment-level (they depend on the OpenAPI
        # surface, not the individual model id), so compute them once.
        mode = detect_mode_from_paths(paths, model_ids[0], extra_args)
        provider = detect_provider(owned_by or "", model_ids[0], paths)

        models: Dict[str, OicmModel] = {}
        for model_id in model_ids:
            model = OicmModel(
                uuid=uuid,
                model_id=model_id,
                model_name=sanitize_model_id(model_id),
                namespace=NAMESPACE,
                ready_replicas=ready,
                total_replicas=total,
                mode=mode,
                provider=provider,
                extra_args=extra_args,
                source="local",
            )
            models[model.composite_key] = model
        return models

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
            logger.debug(f"Failed to query /v1/models for {uuid}: {e}")

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
            logger.debug(f"Failed to read ConfigMap {cm_name}: {e}")
            return None

    async def _query_v1_models(self, uuid: str) -> tuple[list[str], Optional[str]]:
        url = f"http://s-{uuid}.{NAMESPACE}.{CLUSTER_DOMAIN}:{MODEL_PORT}/v1/models"
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            resp = await http_client.get(url)
            if resp.status_code == 405:
                logger.info(
                    f"Model {uuid} returned 405 on /v1/models, non-OpenAI, skipping"
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
            async with httpx.AsyncClient(timeout=5.0) as http_client:
                resp = await http_client.get(url)
                if resp.status_code != 200:
                    return frozenset()
                data = resp.json()
                return frozenset(data.get("paths", {}).keys())
        except Exception as e:
            logger.debug(f"Failed to probe /openapi.json for {uuid}: {e}")
            return frozenset()
