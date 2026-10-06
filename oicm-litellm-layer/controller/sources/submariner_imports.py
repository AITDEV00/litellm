import asyncio
import logging
from typing import Dict, Optional

import httpx
from kubernetes import client

from ..config import (
    MODEL_DEPLOYMENT_TYPE,
    NAMESPACE,
    REMOTE_TIMEOUT_SECONDS,
    WORKLOAD_ID_LABEL,
    WORKLOAD_TYPE_LABEL,
)
from ..models import OicmModel, build_model, detect_mode, parse_model_list
from .base import ModelSource
from .local_deployments import load_kube_config

logger = logging.getLogger("oicm-discovery")

LIGHTHOUSE_LABEL = "endpointslice.kubernetes.io/managed-by"
LIGHTHOUSE_VALUE = "lighthouse-agent.submariner.io"
SOURCE_CLUSTER_LABEL = "multicluster.kubernetes.io/source-cluster"
SERVICE_NAME_LABEL = "multicluster.kubernetes.io/service-name"


class SubmarinerImportSource(ModelSource):
    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        try:
            load_kube_config()
        except Exception as e:
            logger.error("Failed to load kube config: %s", e)
            raise

        self.discovery_api = client.DiscoveryV1Api()
        self._client = http_client or httpx.AsyncClient(timeout=REMOTE_TIMEOUT_SECONDS)

    async def aclose(self) -> None:
        await self._client.aclose()

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

            model_ids = await self._query_v1_models(globalnet_ip, port)
            if not model_ids:
                model_ids = [uuid]
                logger.warning(
                    "Could not discover model_id for %s, using UUID as fallback",
                    composite_uuid,
                )

            api_base_override = f"http://{globalnet_ip}:{port}/v1"

            for model_id in model_ids:
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
                model = build_model(
                    uuid=composite_uuid,
                    model_id=model_id,
                    ready_replicas=1,
                    total_replicas=1,
                    mode=detect_mode(model_id, ""),
                    source=f"submariner:{source_cluster}",
                    api_base_override=api_base_override,
                )
                models[model.deployment_id] = model
                logger.info(
                    "Discovered Submariner import: %s (cluster=%s, ip=%s)",
                    model.model_name,
                    source_cluster,
                    globalnet_ip,
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
    ) -> list[str]:
        url = f"http://{globalnet_ip}:{port}/v1/models"
        try:
            resp = await self._client.get(url)
            if resp.status_code == 405:
                logger.info(
                    "Model at %s:%s returned 405 on /v1/models, non-OpenAI, skipping",
                    globalnet_ip,
                    port,
                )
                return []
            resp.raise_for_status()
            return parse_model_list(resp.json())
        except Exception as e:
            logger.debug(
                "Failed to query /v1/models at %s:%s: %s", globalnet_ip, port, e
            )
        return []
