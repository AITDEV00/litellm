import asyncio
import logging
from dataclasses import replace
from typing import Dict, List, Optional

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
from .models import OicmModel
from .pricing import PricingResolver, PricingSource, pricing_to_params
from .reconciler import SyncReconciler, _summaries_to_models
from .sources import ModelSource
from .sources.local_deployments import LocalDeploymentSource
from .sources.submariner_imports import SubmarinerImportSource
from .status_poller import StatusPoller
from .status_persister import StatusPersister
from .status_sources import build_status_sources

logger = logging.getLogger("oicm-discovery")


class DiscoveryController:
    def __init__(
        self,
        sources: List[ModelSource] | None = None,
        litellm: LiteLLMClient | None = None,
        status_poller: StatusPoller | None = None,
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
        self.pricing_source = PricingSource(
            base_url=self.litellm.base_url,
            headers=self.litellm.headers,
        )
        self.pricing_resolver = PricingResolver(self.pricing_source)
        self.reconciler = SyncReconciler(self.litellm, self.pricing_resolver)
        self.fallback_client = FallbackClient(
            base_url=self.litellm.base_url,
            headers=self.litellm.headers,
        )
        self.fallback_reconciler = FallbackReconciler(self.fallback_client)
        self.status_poller = status_poller or StatusPoller(
            build_status_sources(),
            persister=StatusPersister(self.litellm),
        )
        self._state: Dict[str, OicmModel] = {}
        self._litellm_id_map: Dict[str, str] = {}
        self._running = False

    async def start(self):
        logger.info("Starting OICM Discovery Controller")
        self._running = True
        self._runner = web.AppRunner(web.Application())
        self._runner.app.router.add_get("/health", self._health)
        self._runner.app.router.add_get("/status", self._status)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", HEALTH_PORT)
        await self._site.start()
        logger.info("Health server listening on :%s", HEALTH_PORT)
        # Prime the snapshots so the first reconcile already has OICM's view of
        # existence. Without this, a Stopped deployment would look deleted until
        # the first poll lands.
        if self.status_poller.enabled:
            await self.status_poller.refresh()
        await self.full_sync()
        await asyncio.gather(
            self._watch_loop(),
            self._periodic_resync(),
            self.status_poller.run(),
        )

    async def stop(self):
        self._running = False
        self.status_poller.stop()
        if hasattr(self, "_runner"):
            await self._runner.cleanup()
        for closeable in (self.litellm, self.pricing_source, self.fallback_client):
            await closeable.aclose()
        await self.status_poller.aclose()
        for source in self.sources:
            await source.aclose()
        logger.info("Stopped OICM Discovery Controller")

    async def _health(self, _request):
        return web.Response(text="ok")

    async def _status(self, _request):
        """Expose the latest polled status per workload as JSON.

        Read-only view for verifying that every deployment's status is visible
        without a LiteLLM round trip.
        """
        body = {
            workload_id: {
                "source": self.status_poller.source_of.get(workload_id),
                "cluster": snap.cluster,
                "source_status": snap.source_status.value if snap.source_status else None,
                "serving_available": snap.serving_available,
                "desired_replicas": snap.desired_replicas,
                "available_replicas": snap.available_replicas,
                "unavailable_replicas": snap.unavailable_replicas,
                "error_msg": snap.error_msg,
                "status_changed_at": snap.status_changed_at,
                "observed_at": snap.observed_at,
                "checked_at": self._checked_at(snap.cluster),
            }
            for workload_id, snap in self.status_poller.snapshots.items()
        }
        return web.json_response(body)

    def _checked_at(self, cluster: str) -> Optional[str]:
        """When this source was last polled successfully, if the persister ran."""
        if self.status_poller.persister is None:
            return None
        return self.status_poller.persister.checked_at.get(cluster)

    async def full_sync(self):
        logger.info("Starting full sync...")

        discovered: Dict[str, OicmModel] = {}
        for source in self.sources:
            try:
                models = await source.discover()
                discovered.update(models)
                logger.info(
                    "Source %s: discovered %d models",
                    source.__class__.__name__,
                    len(models),
                )
            except Exception as e:
                logger.error("Source %s failed: %s", source.__class__.__name__, e)

        litellm_by_key = await self.litellm.list_all_models_by_key()

        # Existence comes from the poller's latest snapshots, not from a fresh
        # fetch here. The poll loop is the single writer, so this reads the same
        # map the poller just produced instead of issuing a second identical
        # call and blocking the reconcile on OICM.
        oicm_models = _summaries_to_models(self.status_poller.snapshots)

        plan = await self.reconciler.compute_plan(
            discovered,
            litellm_by_key,
            oicm_models,
            allow_deletes=self.status_poller.all_sources_ok,
        )
        await self.reconciler.execute(plan)

        self._state = plan.new_state
        self._litellm_id_map = plan.new_id_map
        logger.info("Full sync complete: %d models registered", len(self._state))

        await self.fallback_reconciler.reconcile()

    async def _watch_loop(self):
        while self._running:
            try:
                await self._watch_once()
            except Exception as e:
                logger.error("Watch error: %s, reconnecting in 5s...", e)
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
            logger.info("Watch event: %s deployment j-%s", event_type, uuid[:8])
            if event_type == "ADDED":
                await self._handle_add(uuid, dep)
            elif event_type == "DELETED":
                await self._handle_delete(uuid)
            elif event_type == "MODIFIED":
                await self._handle_modify(uuid, dep)

    async def _handle_add(self, uuid: str, dep):
        if not self._running:
            return

        models = await self.local_source.discover_for_deployment(dep)
        if not models:
            logger.warning("No models discovered for j-%s", uuid[:8])
            return

        serving = (dep.status.ready_replicas or 0) > 0
        for key, discovered in models.items():
            model = discovered if serving else replace(discovered, serving=False)
            if key in self._state:
                # A redeploy can reuse the uuid, so the k8s object is the real
                # record and has to replace any non-serving placeholder from
                # OICM rather than be skipped as a duplicate.
                self._state[key] = model
                continue
            pricing = await self.pricing_resolver.resolve(model.model_id)
            inherited = pricing_to_params(pricing)
            litellm_id = await self.litellm.register_model(model, inherited)
            if litellm_id:
                self._litellm_id_map[key] = litellm_id
                self._state[key] = model
                if not serving:
                    # A new row starts routable, and the status poll may be up to
                    # one interval away, so pause it here to avoid a brief window
                    # where a non-serving deployment is selectable. OICM remains
                    # the authority: the next status write reconciles this.
                    await self.litellm.set_blocked(litellm_id, True)

    async def _handle_delete(self, uuid: str):
        """The k8s Deployment is gone. OICM decides whether the row goes.

        A k8s deletion is either a stop or a real delete, and only OICM can tell
        them apart: a Stopped deployment keeps its OICM record, a deleted one
        does not. Routing is left to the status poll, which owns `blocked`, and
        the next full sync removes the row only if OICM no longer lists it.
        """
        model = self._state.get(uuid)
        if model is None:
            logger.warning(
                "Delete event for j-%s but no model in map; "
                "full_sync will clean up on next cycle",
                uuid[:8],
            )
            return

        if model.serving:
            self._state[uuid] = replace(
                model, serving=False, ready_replicas=0, total_replicas=0
            )
        await self.fallback_reconciler.reconcile()

    async def _handle_modify(self, uuid: str, dep):
        ready = dep.status.ready_replicas or 0
        serving = ready > 0

        if uuid in self._state:
            # OicmModel is frozen; replace() is the only way to apply the
            # replica update. `blocked` is not touched here: the status poll
            # owns it from OICM's `serving_available`, and two writers of one
            # column would be two opinions about the same fact.
            previous = self._state[uuid]
            self._state[uuid] = replace(
                previous,
                ready_replicas=ready,
                total_replicas=dep.status.replicas or 0,
                serving=serving,
            )
        else:
            await self._handle_add(uuid, dep)

    async def _periodic_resync(self):
        while self._running:
            await asyncio.sleep(SYNC_INTERVAL)
            if self._running:
                try:
                    await self.full_sync()
                except Exception as e:
                    logger.error("Periodic resync failed: %s", e)
