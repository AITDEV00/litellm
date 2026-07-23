import asyncio
import logging
from typing import Dict, List

from aiohttp import web
from kubernetes import watch

from .config import (
    ENABLE_SUBMARINER_IMPORTS,
    HEALTH_PORT,
    MODEL_DEPLOYMENT_TYPE,
    NAMESPACE,
    SYNC_INTERVAL,
    WATCH_TIMEOUT,
    WORKLOAD_ID_LABEL,
    WORKLOAD_TYPE_LABEL,
)
from .fallbacks import FallbackReconciler
from .fallbacks.client import FallbackClient
from .litellm_client import LiteLLMClient
from .models import OicmModel, detect_mode, sanitize_model_id
from .pricing import PricingResolver, PricingSource, pricing_to_params
from .reconciler import SyncReconciler
from .sources import ModelSource
from .sources.local_deployments import LocalDeploymentSource
from .sources.submariner_imports import SubmarinerImportSource

logger = logging.getLogger("oicm-discovery")


class DiscoveryController:
    def __init__(
        self,
        sources: List[ModelSource] | None = None,
        litellm: LiteLLMClient | None = None,
    ):
        if sources is not None:
            self.sources = sources
        else:
            self.sources: List[ModelSource] = [LocalDeploymentSource()]
            if ENABLE_SUBMARINER_IMPORTS:
                self.sources.append(SubmarinerImportSource())
                logger.info("Submariner import source enabled")
            else:
                logger.info("Submariner import source disabled")

        self.local_source = self.sources[0]

        self.litellm = litellm or LiteLLMClient()
        self.pricing_resolver = PricingResolver(
            PricingSource(
                base_url=self.litellm.base_url,
                headers=self.litellm.headers,
            )
        )
        self.reconciler = SyncReconciler(self.litellm, self.pricing_resolver)
        self.fallback_reconciler = FallbackReconciler(
            FallbackClient(
                base_url=self.litellm.base_url,
                headers=self.litellm.headers,
            )
        )
        self._state: Dict[str, OicmModel] = {}
        self._litellm_id_map: Dict[str, str] = {}
        self._running = False

    async def start(self):
        logger.info("Starting OICM Discovery Controller")
        self._running = True
        self._runner = web.AppRunner(web.Application())
        self._runner.app.router.add_get("/health", self._health)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", HEALTH_PORT)
        await self._site.start()
        logger.info(f"Health server listening on :{HEALTH_PORT}")
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

        discovered: Dict[str, OicmModel] = {}
        for source in self.sources:
            try:
                models = await source.discover()
                discovered.update(models)
                logger.info(
                    f"Source {source.__class__.__name__}: "
                    f"discovered {len(models)} models"
                )
            except Exception as e:
                logger.error(
                    f"Source {source.__class__.__name__} failed: {e}"
                )

        litellm_by_uuid = await self.litellm.list_all_models_by_uuid()

        plan = await self.reconciler.compute_plan(discovered, litellm_by_uuid)
        await self.reconciler.execute(plan)

        self._state = plan.new_state
        self._litellm_id_map = plan.new_id_map
        logger.info(f"Full sync complete: {len(self._state)} models registered")

        await self.fallback_reconciler.reconcile()

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
                    self.local_source.apps_api.list_namespaced_deployment,
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
            if not self._running:
                break
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
        if not self._running:
            return
        if uuid in self._state:
            logger.debug(f"Deployment j-{uuid[:8]} already tracked, skipping")
            return

        ready = dep.status.ready_replicas or 0
        if ready == 0:
            logger.info(f"Deployment j-{uuid[:8]} not ready yet, skipping")
            return

        model_id = await self.local_source.discover_model_id(uuid)
        extra_args = await self.local_source.get_configmap_field(uuid, "EXTRA_ARGS") or ""

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
            source="local",
        )

        if mode == "tts_skip":
            logger.info(f"Skipping TTS model {uuid[:8]}")
            return

        pricing = await self.pricing_resolver.resolve(model.model_id)
        inherited = pricing_to_params(pricing)
        litellm_id = await self.litellm.register_model(model, inherited)
        if litellm_id:
            self._litellm_id_map[uuid] = litellm_id
            self._state[uuid] = model

    async def _handle_delete(self, uuid: str):
        litellm_id = self._litellm_id_map.pop(uuid, None)
        if litellm_id:
            await self.litellm.deregister_model(litellm_id)
            self._state.pop(uuid, None)
            await self.fallback_reconciler.reconcile()
        else:
            logger.warning(
                f"Delete event for j-{uuid[:8]} but no litellm_id in map; "
                f"full_sync will clean up on next cycle"
            )

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
