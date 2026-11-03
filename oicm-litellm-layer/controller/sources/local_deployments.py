import asyncio
import logging
import os
from typing import Dict, Final, Optional, Tuple

import httpx
from kubernetes import client, config

from ..capabilities import (
    CapabilityFacts,
    facts_from_sglang_model_info,
    probe_verdict_to_fact,
)
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

# A tiny solid-red 1x1 PNG and a 0.1s 8kHz silent WAV, both under 100 bytes
# base64-encoded, so the synthetic capability probe costs the backend a few
# dozen tokens and never a real inference.
_SYNTH_IMAGE_URL: Final[str] = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_SYNTH_AUDIO_B64: Final[str] = (
    "UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA="
)


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
        # Synthetic-probe verdicts memoized per deployment for the controller's
        # lifetime: the probe is the only expensive step in discovery (a real
        # inference), so it runs once per deployment per process, never per cycle.
        self._synthetic_probe_cache: Dict[str, Tuple[Optional[bool], Optional[bool]]] = {}

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

        model_ids, owned_by, max_model_len, served_task_types = await self._discover_model_ids(uuid)
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

        capability_facts = await self._probe_capability_facts(uuid, mode, model_ids[0], served_task_types)

        model = build_model(
            uuid=uuid,
            model_id=model_ids[0],
            ready_replicas=ready,
            total_replicas=total,
            mode=mode,
            provider=provider,
            extra_args=extra_args,
            api_surface=api_surface,
            max_model_len=max_model_len,
            capability_facts=capability_facts,
        )
        return {model.deployment_id: model}

    async def _discover_model_ids(
        self, uuid: str
    ) -> tuple[list[str], Optional[str], Optional[int], frozenset[str]]:
        """Return (model_ids, owned_by, max_model_len, served_task_types).

        Precedence: an explicit ConfigMap MODEL_ID (single, backward compat)
        wins over the ids the backend advertises, but the backend is still
        probed for its advertised window, because the token-budget stamp is
        derived from it and a ConfigMap-selected id says nothing about the
        window. A probe failure degrades to no window (no budgets stamped),
        never to a wrong one.
        """
        cm_model_id = await self._get_configmap_field(uuid, "MODEL_ID")
        if cm_model_id and cm_model_id.strip():
            try:
                _, _, max_model_len, task_types = await self._query_v1_models(uuid)
            except Exception as e:
                logger.debug("Failed to query /v1/models for %s: %s", uuid, e)
                return [cm_model_id.strip()], None, None, frozenset()
            return [cm_model_id.strip()], None, max_model_len, task_types

        try:
            return await self._query_v1_models(uuid)
        except Exception as e:
            logger.debug("Failed to query /v1/models for %s: %s", uuid, e)

        return [], None, None, frozenset()

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

    async def _probe_capability_facts(
        self, uuid: str, mode: str, model_id: str, served_task_types: frozenset[str]
    ) -> Optional[CapabilityFacts]:
        """What the deployment itself says it accepts as chat content.

        Precedence: the backend's own modality metadata (sglang serves
        ``/model_info`` with server-computed flags; free and authoritative),
        then one synthetic image + audio chat request for servers that expose
        no metadata (vLLM). Both degrade to unknown (claim nothing) on failure;
        only the synthetic verdicts are memoized, since the metadata probe is
        as cheap as the /v1/models one already running each cycle.
        """
        if mode != "chat":
            return None

        base = f"http://s-{uuid}.{NAMESPACE}.{CLUSTER_DOMAIN}:{MODEL_PORT}"
        for path in ("/model_info", "/get_model_info"):
            try:
                resp = await self._client.get(f"{base}{path}")
            except Exception as e:
                logger.debug("Capability probe %s failed for %s: %s", path, uuid, e)
                continue
            if resp.status_code == 200:
                facts = facts_from_sglang_model_info(resp.json(), served_task_types)
                if facts.accepts_image is not None or facts.accepts_audio is not None:
                    return facts
                # A 200 with no modality flags is a metadata surface that has
                # nothing to say; fall through to the synthetic probe.
                break

        if uuid in self._synthetic_probe_cache:
            image, audio = self._synthetic_probe_cache[uuid]
            return CapabilityFacts(accepts_image=image, accepts_audio=audio)
        image = await self._synthetic_content_probe(uuid, model_id, "image")
        audio = await self._synthetic_content_probe(uuid, model_id, "audio")
        if image is None or audio is None:
            # An inconclusive verdict is not memoized: the cache is the
            # "do not pay the inference again" guard, and an unknown would
            # freeze as a permanent no-claim. Retry on the next cycle.
            return CapabilityFacts(accepts_image=image, accepts_audio=audio)
        self._synthetic_probe_cache[uuid] = (image, audio)
        return CapabilityFacts(accepts_image=image, accepts_audio=audio)

    async def _synthetic_content_probe(
        self, uuid: str, model_id: str, kind: str
    ) -> Optional[bool]:
        """One minimal chat request carrying one synthetic image or audio part.

        Sent straight to the pod, never through the gateway: no key, no spend
        row, no router reload. A 200 is acceptance, an explicit 4xx is
        rejection, anything else is unknown and retried on a later cycle (the
        memoization only lands once both probes have a definitive verdict).
        """
        if kind == "image":
            content = [
                {"type": "text", "text": "Describe this image in one word."},
                {"type": "image_url", "image_url": {"url": _SYNTH_IMAGE_URL}},
            ]
        else:
            content = [
                {"type": "text", "text": "Transcribe this audio in one word."},
                {"type": "input_audio", "input_audio": {"data": _SYNTH_AUDIO_B64, "format": "wav"}},
            ]
        url = (
            f"http://s-{uuid}.{NAMESPACE}.{CLUSTER_DOMAIN}:{MODEL_PORT}"
            "/v1/chat/completions"
        )
        try:
            resp = await self._client.post(
                url,
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": content}],
                    "max_tokens": 8,
                    "stream": False,
                },
            )
        except Exception as e:
            logger.debug("Synthetic %s probe failed for %s: %s", kind, uuid, e)
            return None
        verdict = probe_verdict_to_fact(resp.status_code)
        if verdict is None:
            logger.debug(
                "Synthetic %s probe inconclusive for %s (HTTP %d); retrying next cycle",
                kind,
                uuid,
                resp.status_code,
            )
            return None
        return verdict

    async def _query_v1_models(
        self, uuid: str
    ) -> tuple[list[str], Optional[str], Optional[int], frozenset[str]]:
        """(ids, owned_by, max_model_len, served_task_types) from /v1/models.

        The served task types ride the same request the window already uses:
        a TI2I-only server self-declares here, and that is what suppresses
        its capability stamp (see capabilities.py).
        """
        url = f"http://s-{uuid}.{NAMESPACE}.{CLUSTER_DOMAIN}:{MODEL_PORT}/v1/models"
        resp = await self._client.get(url)
        if resp.status_code == 405:
            logger.info(
                "Model %s returned 405 on /v1/models, non-OpenAI, skipping", uuid
            )
            return [], None, None, frozenset()
        resp.raise_for_status()
        data = resp.json()
        model_ids = parse_model_list(data)
        owned_by = None
        max_model_len: Optional[int] = None
        task_types: frozenset[str] = frozenset()
        openai_data = data.get("data") if isinstance(data, dict) else None
        if isinstance(openai_data, list) and openai_data and isinstance(openai_data[0], dict):
            first = openai_data[0]
            owned_by = first.get("owned_by", "")
            raw_len = first.get("max_model_len")
            if isinstance(raw_len, int) and not isinstance(raw_len, bool) and raw_len > 0:
                max_model_len = raw_len
            raw_tasks = first.get("supported_task_types")
            if isinstance(raw_tasks, list):
                task_types = frozenset(t for t in raw_tasks if isinstance(t, str))
        return model_ids, owned_by, max_model_len, task_types

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
