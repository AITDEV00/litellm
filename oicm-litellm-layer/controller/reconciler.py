import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .litellm_client import LiteLLMClient
from .models import OicmModel, to_litellm_mode
from .pricing import PricingResolver, pricing_to_params

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


class SyncReconciler:
    def __init__(self, litellm: LiteLLMClient, pricing: PricingResolver):
        self.litellm = litellm
        self.pricing = pricing

    async def compute_plan(
        self,
        k8s_models: Dict[str, OicmModel],
        litellm_by_key: Dict[str, List[dict]],
    ) -> SyncPlan:
        k8s_keys = set(k8s_models.keys())
        litellm_keys = set(litellm_by_key.keys())
        plan = SyncPlan()
        # Richest existing entry per shared key. Built here rather than by
        # trimming litellm_by_key in place, so the caller's dict is untouched.
        best_entry_by_key: Dict[str, dict] = {}

        for key in litellm_keys:
            entries = litellm_by_key[key]

            if key not in k8s_keys:
                for e in entries:
                    mid = e.get("model_id")
                    if mid:
                        plan.deletes.append(mid)
                continue

            best_entry, loser_ids = _pick_richest_entry(entries)
            plan.deletes.extend(loser_ids)
            plan.new_id_map[key] = best_entry.get("model_id")
            best_entry_by_key[key] = best_entry

        for key in k8s_keys - litellm_keys:
            model = k8s_models[key]
            if not model.is_ready:
                continue
            pricing = await self.pricing.resolve(model.model_id)
            plan.registers.append((model, pricing_to_params(pricing)))

        for key in k8s_keys & litellm_keys:
            model = k8s_models[key]
            existing_id = plan.new_id_map.get(key)
            existing_entry = best_entry_by_key[key]
            existing_model_name = existing_entry.get("model_name", "")

            if existing_model_name != model.model_name:
                if existing_id:
                    plan.deletes.append(existing_id)
                pricing = await self.pricing.resolve(model.model_id)
                plan.registers.append((model, pricing_to_params(pricing)))
                continue

            if existing_id:
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
                plan.new_id_map[model.composite_key] = litellm_id
                plan.new_state[model.composite_key] = model

        if plan.patches:
            logger.info("Patched %d/%d models", patched, len(plan.patches))

        return deleted, len(registered_ids), patched
