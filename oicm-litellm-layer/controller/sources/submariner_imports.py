import asyncio
import logging
import os
from typing import Dict, Optional

import httpx
from kubernetes import client, config

from ..config import (
    MODEL_DEPLOYMENT_TYPE,
    NAMESPACE,
    WORKLOAD_ID_LABEL,
    WORKLOAD_TYPE_LABEL,
)
from ..models import OicmModel, detect_mode, sanitize_model_id
from .base import ModelSource

logger = logging.getLogger("oicm-discovery")

LIGHTHOUSE_LABEL = "endpointslice.kubernetes.io/managed-by"
LIGHTHOUSE_VALUE = "lighthouse-agent.submariner.io"
SOURCE_CLUSTER_LABEL = "multicluster.kubernetes.io/source-cluster"
SERVICE_NAME_LABEL = "multicluster.kubernetes.io/service-name"


class SubmarinerImportSource(ModelSource):
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

        self.discovery_api = client.DiscoveryV1Api()

    async def discover(self) -> Dict[str, OicmModel]:
        loop = asyncio.get_event_loop()
        label_selector = (
            f"{LIGHTHOUSE_LABEL}={LIGHTHOUSE_VALUE},"
            f"{WORKLOAD_TYPE_LABEL}={MODEL_DEPLOYMENT_TYPE}"
        )
        endpoint_slices = await loop.run_in_executor(
            None,
            lambda: self.discovery_api.list_namespaced_endpoint_slice(
                namespace=NAMESPACE,
                label_selector=label_selector,
            ),
        )

        models: Dict[str, OicmModel] = {}
        for es in endpoint_slices.items:
            labels = es.metadata.labels or {}
            addresses = self._extract_addresses(es)
            if not addresses:
                continue

            port = self._extract_port(es)
            if port is None:
                continue

            globalnet_ip = addresses[0]
            source_cluster = labels.get(SOURCE_CLUSTER_LABEL, "unknown")
            service_name = labels.get(SERVICE_NAME_LABEL, "")
            workload_id = labels.get(WORKLOAD_ID_LABEL, "")
            uuid = workload_id or service_name
            if not uuid:
                continue

            composite_uuid = f"submariner:{source_cluster}:{uuid}"

            model_id = await self._query_v1_models(globalnet_ip, port)
            if not model_id:
                model_id = uuid
                logger.warning(
                    f"Could not discover model_id for {composite_uuid}, "
                    f"using UUID as fallback"
                )

            # The model_name is the raw model_id from the upstream /v1/models endpoint
            # (e.g. "zai-org/GLM-5.2-FP8"). We deliberately do NOT prefix it with the
            # source cluster name. The model_id is already globally unique across clusters
            # (it comes from the HuggingFace model registry), and adding a cluster prefix
            # (e.g. "abudhabi-zai-org/GLM-5.2-FP8") would make the model name differ from
            # what clients expect, breaking compatibility with any code that references
            # models by their canonical HuggingFace IDs.
            #
            # If two clusters ever serve the same model_id, the controller will register
            # them under the same LiteLLM model name with different api_base overrides,
            # and LiteLLM's router will load-balance across them. That is the desired
            # behavior, not a collision to avoid with a prefix.
            model_name = sanitize_model_id(model_id)
            mode = detect_mode(model_id, "")
            api_base_override = f"http://{globalnet_ip}:{port}/v1"

            models[composite_uuid] = OicmModel(
                uuid=composite_uuid,
                model_id=model_id,
                model_name=model_name,
                namespace=NAMESPACE,
                ready_replicas=1,
                total_replicas=1,
                mode=mode,
                source=f"submariner:{source_cluster}",
                api_base_override=api_base_override,
            )
            logger.info(
                f"Discovered Submariner import: {model_name} "
                f"(cluster={source_cluster}, ip={globalnet_ip})"
            )

        return models

    def _extract_addresses(self, endpoint_slice) -> list:
        addresses = []
        for ep in endpoint_slice.endpoints or []:
            if ep and ep.addresses:
                addresses.extend(ep.addresses)
        return addresses

    def _extract_port(self, endpoint_slice) -> Optional[int]:
        ports = endpoint_slice.ports or []
        if ports and ports[0].port:
            return ports[0].port
        return None

    async def _query_v1_models(
        self, globalnet_ip: str, port: int
    ) -> Optional[str]:
        url = f"http://{globalnet_ip}:{port}/v1/models"
        try:
            async with httpx.AsyncClient(timeout=10.0) as http_client:
                resp = await http_client.get(url)
                if resp.status_code == 405:
                    logger.info(
                        f"Model at {globalnet_ip}:{port} returned 405 on "
                        f"/v1/models, non-OpenAI, skipping"
                    )
                    return None
                resp.raise_for_status()
                data = resp.json()
                models = data.get("data", [])
                if models:
                    return models[0]["id"]
        except Exception as e:
            logger.debug(f"Failed to query /v1/models at {globalnet_ip}:{port}: {e}")
        return None
