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
from ..models import OicmModel, detect_mode, sanitize_model_id
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

            ready = dep.status.ready_replicas or 0
            total = dep.status.replicas or 0

            model_id = await self._discover_model_id(uuid)
            if not model_id:
                model_id = uuid
                logger.warning(
                    f"Could not discover MODEL_ID for {uuid}, using fallback"
                )

            model_name = sanitize_model_id(model_id)
            extra_args = await self._get_configmap_field(uuid, "EXTRA_ARGS") or ""
            mode = detect_mode(model_id, extra_args)

            models[uuid] = OicmModel(
                uuid=uuid,
                model_id=model_id,
                model_name=model_name,
                namespace=NAMESPACE,
                ready_replicas=ready,
                total_replicas=total,
                mode=mode,
                extra_args=extra_args,
                source="local",
            )

        return models

    async def discover_model_id(self, uuid: str) -> Optional[str]:
        return await self._discover_model_id(uuid)

    async def get_configmap_field(self, uuid: str, field: str) -> Optional[str]:
        return await self._get_configmap_field(uuid, field)

    async def _discover_model_id(self, uuid: str) -> Optional[str]:
        cm_model_id = await self._get_configmap_field(uuid, "MODEL_ID")
        if cm_model_id and cm_model_id.strip():
            return cm_model_id.strip()

        try:
            model_id = await self._query_v1_models(uuid)
            if model_id:
                return model_id
        except Exception as e:
            logger.debug(f"Failed to query /v1/models for {uuid}: {e}")

        return None

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

    async def _query_v1_models(self, uuid: str) -> Optional[str]:
        url = f"http://s-{uuid}.{NAMESPACE}.{CLUSTER_DOMAIN}:{MODEL_PORT}/v1/models"
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            resp = await http_client.get(url)
            if resp.status_code == 405:
                logger.info(
                    f"Model {uuid} returned 405 on /v1/models, non-OpenAI, skipping"
                )
                return None
            resp.raise_for_status()
            data = resp.json()
            models = data.get("data", [])
            if models:
                return models[0]["id"]
        return None
