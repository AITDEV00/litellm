import asyncio
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from .config import NAMESPACE
from .litellm_client import LiteLLMClient
from .models import OicmModel, build_oicm_model, to_litellm_mode
from .pricing import PricingResolver, pricing_to_params
from .status.snapshot import OicmStatusSnapshot

logger = logging.getLogger("oicm-discovery")

CONFIG_KEYS = {
    "rpm",
    "tpm",
    "max_parallel_requests",
    "input_cost_per_token",
    "output_cost_per_token",
    "input_cost_per_second",
    "output_cost_per_second",
}


@dataclass
class SyncPlan:
    deletes: List[str] = field(default_factory=list)
    registers: List[Tuple[OicmModel, Optional[dict]]] = field(default_factory=list)
    patches: List[Tuple[str, dict, Optional[dict]]] = field(default_factory=list)
    # (model_id, blocked) to flip routing for a registered model. Separate from
    # patches because `blocked` is a top-level column, not a litellm_params key,
    # and it must be settable on a model whose config is otherwise unchanged.
    blocks: List[Tuple[str, bool]] = field(default_factory=list)
    new_state: Dict[str, OicmModel] = field(default_factory=dict)
    new_id_map: Dict[str, str] = field(default_factory=dict)


def _subset_matches(stored: object, patch: dict) -> bool:
    """True when every key the patch would set already holds that value.

    A patch is only worth issuing when it would actually change something: every
    gateway write triggers a full model reload on every replica and bumps
    model_info.updated_at, which forces the router to swap the deployment. The
    controller re-probes every deployment each cycle and most cycles find the
    same config, so without this check a steady-state cluster rewrites every
    model every cycle for no change.

    Only the patch's keys are compared, not the whole stored object, because
    LiteLLM merges the patch into the stored params rather than replacing them.
    """
    if not isinstance(stored, dict):
        return False
    return all(stored.get(key) == value for key, value in patch.items())


def _patch_is_noop(
    existing_entry: dict, patch_params: dict, patch_model_info: Optional[dict]
) -> bool:
    """True when the patch would leave the deployment exactly as it is."""
    if not _subset_matches(existing_entry.get("litellm_params"), patch_params):
        return False
    if patch_model_info is None:
        return True
    return _subset_matches(existing_entry.get("model_info"), patch_model_info)


def _blocked_matches(existing_entry: dict, blocked: bool) -> bool:
    """True when the stored entry already carries this routing state.

    LiteLLM surfaces the `blocked` column inside model_info, so that is where
    the current value is read from. An entry that has never been blocked and has
    no flag at all reads as False, which is why the default is compared rather
    than treated as unknown.
    """
    return (existing_entry.get("model_info") or {}).get("blocked", False) is blocked


def _pick_richest_entry(entries: List[dict]) -> Tuple[dict, List[str]]:
    if len(entries) == 1:
        return entries[0], []

    best_idx = 0
    best_score = -1
    for i, entry in enumerate(entries):
        params = entry.get("litellm_params", {}) or {}
        score = sum(1 for k in CONFIG_KEYS if params.get(k) is not None)
        if score > best_score:
            best_score = score
            best_idx = i

    loser_ids = [
        entries[i]["model_id"]
        for i in range(len(entries))
        if i != best_idx and entries[i].get("model_id")
    ]
    return entries[best_idx], loser_ids


def _oicm_managed(existing_entry: dict) -> bool:
    """True when the controller registered this row and may therefore remove it.

    The marker is an ``oicm_uuid``, which every controller-registered row has,
    local or cross-cluster import alike. An admin-added model and a heartbeat
    row both lack one, so neither is the controller's to delete when OICM stops
    listing it.
    """
    return bool((existing_entry.get("model_info") or {}).get("oicm_uuid"))


def _summaries_to_models(
    summaries: Mapping[str, OicmStatusSnapshot],
) -> Dict[str, OicmModel]:
    """Build a model record for each deployment OICM still lists.

    A deployment OICM reports as Stopped has no k8s Deployment and no Service,
    so the watch cannot see it and it cannot be re-probed. It still has to stay
    registered and visible, so existence comes from OICM instead: this turns
    each summary into the same record shape discovery would have produced and
    marks it non-serving, which is what pauses its routing.
    """
    return {
        workload_id: build_oicm_model(
            uuid=workload_id,
            model_id=workload_id,
            model_name=workload_id,
            namespace=NAMESPACE,
            serving=snap.serving_available,
        )
        for workload_id, snap in summaries.items()
    }


def _merge_models(
    k8s_models: Dict[str, OicmModel],
    oicm_models: Dict[str, OicmModel],
) -> Dict[str, OicmModel]:
    """One record per deployment, k8s discovery winning where both know it.

    The k8s record carries the real model id, provider, mode, and api_base, so
    it is always preferred. The OICM record only fills in the deployments the
    watch cannot see, which is exactly the Stopped set.
    """
    merged = dict(oicm_models)
    merged.update(k8s_models)
    return merged


class SyncReconciler:
    def __init__(self, litellm: LiteLLMClient, pricing: PricingResolver):
        self.litellm = litellm
        self.pricing = pricing

    async def compute_plan(
        self,
        k8s_models: Dict[str, OicmModel],
        litellm_by_key: Dict[str, List[dict]],
        oicm_models: Optional[Dict[str, OicmModel]] = None,
        allow_deletes: bool = True,
    ) -> SyncPlan:
        # Existence comes from OICM, not from the k8s watch: a Stopped
        # deployment has no k8s object but must stay registered, and a
        # deployment absent from OICM has been deleted and must go. The watch
        # only supplies the live record for deployments that still exist, so
        # when OICM is unavailable the watch remains the sole source and the
        # delete rule stays scoped to controller-managed rows.
        #
        # `allow_deletes` is the guard that keeps an incomplete view from being
        # read as deletion: when any configured source failed to poll, its
        # deployments are missing from `oicm_models` through no fault of their
        # own, so nothing is removed this cycle. The next complete cycle catches
        # up, and a spurious delete is far worse than a delayed one.
        oicm_models = oicm_models if oicm_models is not None else {}
        desired = _merge_models(k8s_models, oicm_models)
        oicm_keys = set(oicm_models.keys())

        k8s_keys = set(k8s_models.keys())
        litellm_keys = set(litellm_by_key.keys())
        plan = SyncPlan()
        # Richest existing entry per shared key. Built here rather than by
        # trimming litellm_by_key in place, so the caller's dict is untouched.
        best_entry_by_key: Dict[str, dict] = {}

        for key in litellm_keys:
            entries = litellm_by_key[key]

            if key in k8s_keys:
                best_entry, loser_ids = _pick_richest_entry(entries)
                plan.deletes.extend(loser_ids)
                plan.new_id_map[key] = best_entry.get("model_id")
                best_entry_by_key[key] = best_entry
                continue

            if key in oicm_keys:
                # Known to OICM but not to the watch: the deployment is Stopped
                # (or otherwise not serving). Keep it registered so Stopped and
                # Deleted stay distinguishable, and let the loop below pause it.
                best_entry, loser_ids = _pick_richest_entry(entries)
                plan.deletes.extend(loser_ids)
                plan.new_id_map[key] = best_entry.get("model_id")
                best_entry_by_key[key] = best_entry
                continue

            # Absent from both k8s and OICM: genuinely deleted. Only remove the
            # rows the controller itself created, so an admin-added model or a
            # heartbeat row is never collateral damage, and only when the poll
            # that says so was complete.
            if not allow_deletes:
                continue
            for e in entries:
                if _oicm_managed(e) and e.get("model_id"):
                    plan.deletes.append(e["model_id"])

        for key in k8s_keys - litellm_keys:
            # Only the k8s watch yields a served model id and a resolving
            # api_base. An OICM-only deployment has neither, so registering one
            # would create a permanently-blocked row with nothing behind it.
            model = desired[key]
            pricing = await self.pricing.resolve(model.model_id)
            plan.registers.append((model, pricing_to_params(pricing)))

        for key in desired.keys() & litellm_keys:
            model = desired[key]
            existing_id = plan.new_id_map.get(key)
            existing_entry = best_entry_by_key[key]

            if key not in k8s_keys:
                # Known only from OICM: a Stopped deployment with no k8s object,
                # so there is no live config to reconcile and only its routing
                # state is managed. Its stored name is the real served id, which
                # OICM does not carry, so the name must not be compared here or
                # the row would be churned to the placeholder's uuid.
                #
                # It is deliberately kept out of new_state: the watch handlers
                # key off that map, and a placeholder there would make a later
                # redeploy with the same uuid look like a duplicate ADDED.
                if existing_id:
                    if model.serving:
                        if _blocked_matches(existing_entry, blocked=True):
                            plan.blocks.append((existing_id, False))
                    elif not _blocked_matches(existing_entry, blocked=True):
                        plan.blocks.append((existing_id, True))
                continue

            existing_model_name = existing_entry.get("model_name", "")
            if existing_model_name != model.model_name:
                if existing_id:
                    plan.deletes.append(existing_id)
                pricing = await self.pricing.resolve(model.model_id)
                plan.registers.append((model, pricing_to_params(pricing)))
                continue

            if existing_id:
                # A non-serving deployment keeps its registration and is paused
                # instead, so it stays visible while being excluded from routing.
                if not model.serving:
                    if not _blocked_matches(existing_entry, blocked=True):
                        plan.blocks.append((existing_id, True))
                    plan.new_state[key] = model
                    continue

                patch_params: dict = {
                    "model": f"{model.provider}/{model.model_id}",
                    "api_base": model.api_base,
                }
                if model.api_surface:
                    patch_params["api_surface"] = model.api_surface
                pricing = await self.pricing.resolve(model.model_id)
                inherited = pricing_to_params(pricing)
                if inherited:
                    patch_params.update(inherited)
                patch_model_info: dict = {"mode": to_litellm_mode(model.mode)}
                if not _patch_is_noop(existing_entry, patch_params, patch_model_info):
                    plan.patches.append((existing_id, patch_params, patch_model_info))
                # A deployment that came back must be routable again. This is
                # what makes a stop-then-start round trip work.
                if _blocked_matches(existing_entry, blocked=True):
                    plan.blocks.append((existing_id, False))
            plan.new_state[key] = model

        return plan

    async def execute(self, plan: SyncPlan) -> Tuple[int, int, int]:
        deleted, registered_ids, patched = await self.litellm.batch(
            plan.deletes, plan.registers, plan.patches
        )
        if plan.deletes:
            logger.info("Deleted %d/%d models", deleted, len(plan.deletes))

        # registered_ids preserves input order (None for a failed register), so
        # zip aligns each id to its model by position.
        for (model, _), litellm_id in zip(plan.registers, registered_ids):
            if litellm_id:
                plan.new_id_map[model.deployment_id] = litellm_id
                plan.new_state[model.deployment_id] = model
                # A newly registered model starts unblocked, so a non-serving
                # one has to be paused after it exists. Its id is only known
                # here, which is why this cannot be planned in compute_plan.
                if not model.serving:
                    plan.blocks.append((litellm_id, True))

        if plan.patches:
            logger.info("Patched %d/%d models", patched, len(plan.patches))

        if plan.blocks:
            blocked_results = await asyncio.gather(
                *(self.litellm.set_blocked(mid, blocked) for mid, blocked in plan.blocks)
            )
            blocked = sum(1 for r in blocked_results if r)
            logger.info("Set blocked on %d/%d models", blocked, len(plan.blocks))

        return deleted, len(registered_ids), patched
