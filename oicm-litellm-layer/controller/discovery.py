"""
OICM Discovery Controller: watches K8s deployments and syncs them to LiteLLM.

Component #1 of the OICM->LiteLLM integration layer.
Watches OICM model deployments in the adeo namespace, discovers each model's
identity from ConfigMaps, and registers/deregisters them via LiteLLM REST API.

MODEL_ID discovery strategy:
  Priority 1: ConfigMap configmap-{uuid}-main -> data.MODEL_ID
  Priority 2: GET http://s-{uuid}.{ns}.svc.cluster.local:8080/v1/models
  Fallback:   Use the UUID as the model_name (last resort)
"""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from typing import Dict, Optional

import httpx
from aiohttp import web
from kubernetes import client, config, watch

logger = logging.getLogger("oicm-discovery")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

LITELLM_ADMIN_URL = os.getenv("LITELLM_ADMIN_URL", "http://localhost:4000")
LITELLM_ADMIN_KEY = os.getenv("LITELLM_ADMIN_KEY", "sk-1234")
NAMESPACE = os.getenv("WATCH_NAMESPACE", "adeo")
CLUSTER_DOMAIN = os.getenv("CLUSTER_DOMAIN", "svc.cluster.local")
MODEL_PORT = int(os.getenv("MODEL_PORT", "8080"))
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL", "300"))
WATCH_TIMEOUT = int(os.getenv("WATCH_TIMEOUT", "300"))

WORKLOAD_TYPE_LABEL = "oip/workload-type"
WORKLOAD_ID_LABEL = "oip/workload-id"
MODEL_DEPLOYMENT_TYPE = "model_deployment"


@dataclass
class OicmModel:
    uuid: str
    model_id: str
    model_name: str
    namespace: str
    ready_replicas: int
    total_replicas: int
    mode: str = "chat"
    litellm_model_id: Optional[str] = None
    extra_args: str = ""

    @property
    def api_base(self) -> str:
        return f"http://s-{self.uuid}.{self.namespace}.{CLUSTER_DOMAIN}:{MODEL_PORT}/v1"

    @property
    def is_ready(self) -> bool:
        return self.ready_replicas > 0


def sanitize_model_id(raw_id: str) -> str:
    raw_id = raw_id.strip()
    if raw_id.startswith("/"):
        return raw_id.lstrip("/").replace("/", "--")
    return raw_id


def detect_mode(model_id: str, extra_args: str) -> str:
    mid_lower = model_id.lower()
    extra_lower = extra_args.lower()

    if "embedding" in mid_lower or "--runner pooling" in extra_lower:
        return "embedding"
    if "whisper" in mid_lower or "asr" in mid_lower:
        return "transcription"
    if "tts" in extra_lower or "text_to_speech" in extra_lower:
        return "tts_skip"
    return "chat"


class K8sDiscoverer:
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

    async def list_model_deployments(self) -> Dict[str, OicmModel]:
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
            )

        return models

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


class LiteLLMClient:
    def __init__(
        self, base_url: str = LITELLM_ADMIN_URL, admin_key: str = LITELLM_ADMIN_KEY
    ):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {admin_key}",
            "Content-Type": "application/json",
        }

    async def register_model(self, model: OicmModel) -> Optional[str]:
        if model.mode == "tts_skip":
            logger.info(f"Skipping TTS model {model.uuid} ({model.model_id})")
            return None

        litellm_mode = model.mode
        if litellm_mode == "transcription":
            litellm_mode = "chat"

        payload = {
            "model_name": model.model_name,
            "litellm_params": {
                "model": f"hosted_vllm/{model.model_id}",
                "api_base": model.api_base,
                "api_key": "",
                "drop_params": True,
            },
            "model_info": {
                "mode": litellm_mode,
                "oicm_uuid": model.uuid,
                "oicm_namespace": model.namespace,
            },
        }

        async with httpx.AsyncClient(timeout=10.0) as http_client:
            try:
                resp = await http_client.post(
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

    async def deregister_model(self, litellm_model_id: str) -> bool:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            try:
                resp = await http_client.post(
                    f"{self.base_url}/model/delete",
                    headers=self.headers,
                    json={"id": litellm_model_id},
                )
                resp.raise_for_status()
                logger.info(f"Deregistered model litellm_id={litellm_model_id}")
                return True
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Failed to deregister {litellm_model_id}: {e.response.text}"
                )
                return False

    async def list_models(self) -> Dict[str, dict]:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            try:
                resp = await http_client.get(
                    f"{self.base_url}/model/info",
                    headers=self.headers,
                )
                resp.raise_for_status()
                result = resp.json()
                models = {}
                for m in result.get("data", []):
                    info = m.get("model_info", {})
                    oicm_uuid = info.get("oicm_uuid")
                    if oicm_uuid:
                        models[oicm_uuid] = m
                return models
            except Exception as e:
                logger.error(f"Failed to list models: {e}")
                return {}


class DiscoveryController:
    def __init__(self):
        self.discoverer = K8sDiscoverer()
        self.litellm = LiteLLMClient()
        self._state: Dict[str, OicmModel] = {}
        self._litellm_id_map: Dict[str, str] = {}
        self._running = False

    async def start(self):
        logger.info("Starting OICM Discovery Controller")
        self._running = True
        self._runner = web.AppRunner(web.Application())
        self._runner.app.router.add_get("/health", self._health)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", 8090)
        await self._site.start()
        logger.info("Health server listening on :8090")
        await self.full_sync()
        await asyncio.gather(
            self._watch_loop(),
            self._periodic_resync(),
        )

    async def stop(self):
        self._running = False
        if hasattr(self, "_runner"):
            await self._runner.cleanup()
        logger.info("Stopped OICM Discovery Controller")

    async def _health(self, request):
        return web.Response(text="ok")

    async def full_sync(self):
        logger.info("Starting full sync...")

        k8s_models = await self.discoverer.list_model_deployments()
        litellm_models = await self.litellm.list_models()

        k8s_uuids = set(k8s_models.keys())
        litellm_uuids = set(litellm_models.keys())

        new_uuids = k8s_uuids - litellm_uuids
        for uuid in new_uuids:
            model = k8s_models[uuid]
            if model.is_ready and model.mode != "tts_skip":
                litellm_id = await self.litellm.register_model(model)
                if litellm_id:
                    self._litellm_id_map[uuid] = litellm_id
                    self._state[uuid] = model

        deleted_uuids = litellm_uuids - k8s_uuids
        for uuid in deleted_uuids:
            litellm_id = self._litellm_id_map.get(uuid) or litellm_models[uuid].get(
                "model_id"
            )
            if litellm_id:
                await self.litellm.deregister_model(litellm_id)
                self._litellm_id_map.pop(uuid, None)
                self._state.pop(uuid, None)

        common_uuids = k8s_uuids & litellm_uuids
        for uuid in common_uuids:
            model = k8s_models[uuid]
            old_model = self._state.get(uuid)
            if old_model and old_model.model_id != model.model_id:
                old_litellm_id = self._litellm_id_map.get(uuid)
                if old_litellm_id:
                    await self.litellm.deregister_model(old_litellm_id)
                if model.is_ready and model.mode != "tts_skip":
                    new_litellm_id = await self.litellm.register_model(model)
                    if new_litellm_id:
                        self._litellm_id_map[uuid] = new_litellm_id
                        self._state[uuid] = model

        logger.info(f"Full sync complete: {len(self._state)} models registered")

    async def _watch_loop(self):
        while self._running:
            try:
                await self._watch_once()
            except Exception as e:
                logger.error(f"Watch error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _watch_once(self):
        loop = asyncio.get_event_loop()

        def _do_watch():
            w = watch.Watch()
            events = []
            try:
                for event in w.stream(
                    self.discoverer.apps_api.list_namespaced_deployment,
                    namespace=NAMESPACE,
                    label_selector=f"{WORKLOAD_TYPE_LABEL}={MODEL_DEPLOYMENT_TYPE}",
                    timeout_seconds=WATCH_TIMEOUT,
                ):
                    if not self._running:
                        break
                    events.append(event)
            finally:
                w.stop()
            return events

        events = await loop.run_in_executor(None, _do_watch)
        for event in events:
            event_type = event["type"]
            dep = event["object"]
            uuid = dep.metadata.labels.get(WORKLOAD_ID_LABEL, "")
            if not uuid:
                continue
            logger.info(f"Watch event: {event_type} deployment j-{uuid[:8]}")
            if event_type == "ADDED":
                await self._handle_add(uuid, dep)
            elif event_type == "DELETED":
                await self._handle_delete(uuid)
            elif event_type == "MODIFIED":
                await self._handle_modify(uuid, dep)

    async def _handle_add(self, uuid: str, dep):
        ready = dep.status.ready_replicas or 0
        if ready == 0:
            logger.info(f"Deployment j-{uuid[:8]} not ready yet, skipping")
            return

        model_id = await self.discoverer._discover_model_id(uuid)
        extra_args = (
            await self.discoverer._get_configmap_field(uuid, "EXTRA_ARGS") or ""
        )

        if not model_id:
            model_id = uuid
            logger.warning(f"No MODEL_ID for {uuid[:8]}, using UUID as fallback")

        model_name = sanitize_model_id(model_id)
        mode = detect_mode(model_id, extra_args)

        model = OicmModel(
            uuid=uuid,
            model_id=model_id,
            model_name=model_name,
            namespace=NAMESPACE,
            ready_replicas=ready,
            total_replicas=dep.status.replicas or 0,
            mode=mode,
            extra_args=extra_args,
        )

        if mode == "tts_skip":
            logger.info(f"Skipping TTS model {uuid[:8]}")
            return

        litellm_id = await self.litellm.register_model(model)
        if litellm_id:
            self._litellm_id_map[uuid] = litellm_id
            self._state[uuid] = model

    async def _handle_delete(self, uuid: str):
        litellm_id = self._litellm_id_map.get(uuid)
        if litellm_id:
            await self.litellm.deregister_model(litellm_id)
            self._litellm_id_map.pop(uuid, None)
            self._state.pop(uuid, None)

    async def _handle_modify(self, uuid: str, dep):
        ready = dep.status.ready_replicas or 0
        old_model = self._state.get(uuid)

        if old_model:
            old_model.ready_replicas = ready
            old_model.total_replicas = dep.status.replicas or 0
        elif ready > 0:
            await self._handle_add(uuid, dep)

    async def _periodic_resync(self):
        while self._running:
            await asyncio.sleep(SYNC_INTERVAL)
            if self._running:
                try:
                    await self.full_sync()
                except Exception as e:
                    logger.error(f"Periodic resync failed: {e}")


def run_once():
    """Run a single full sync and exit. Useful for testing."""
    controller = DiscoveryController()
    asyncio.run(controller.full_sync())


def run():
    """Run the controller in continuous mode."""
    controller = DiscoveryController()

    loop = asyncio.new_event_loop()

    def _shutdown(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        loop.create_task(controller.stop())

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        loop.run_until_complete(controller.start())
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(controller.stop())
        loop.close()

if __name__ == "__main__":
    import sys

    if "--once" in sys.argv:
        run_once()
    else:
        run()
