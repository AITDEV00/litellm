import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .litellm_client import LiteLLMClient
from .models import OicmModel
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
    patches: List[Tuple[str, dict]] = field(default_factory=list)
    new_state: Dict[str, OicmModel] = field(default_factory=dict)
    new_id_map: Dict[str, str] = field(default_factory=dict)


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
        litellm_by_uuid: Dict[str, List[dict]],
    ) -> SyncPlan:
        k8s_uuids = set(k8s_models.keys())
        litellm_uuids = set(litellm_by_uuid.keys())
        plan = SyncPlan()

        for uuid in litellm_uuids:
            entries = litellm_by_uuid[uuid]

            if uuid not in k8s_uuids:
                for e in entries:
                    mid = e.get("model_id")
                    if mid:
                        plan.deletes.append(mid)
                continue

            best_entry, loser_ids = _pick_richest_entry(entries)
            plan.deletes.extend(loser_ids)
            plan.new_id_map[uuid] = best_entry.get("model_id")
            litellm_by_uuid[uuid] = [best_entry]

        for uuid in k8s_uuids - litellm_uuids:
            model = k8s_models[uuid]
            if not model.is_ready or model.mode == "tts_skip":
                continue
            pricing = await self.pricing.resolve(model.model_id)
            plan.registers.append((model, pricing_to_params(pricing)))

        for uuid in k8s_uuids & litellm_uuids:
            model = k8s_models[uuid]
            existing_id = plan.new_id_map.get(uuid)

            if model.mode == "tts_skip":
                if existing_id:
                    plan.deletes.append(existing_id)
                    plan.new_id_map.pop(uuid, None)
                continue

            existing_entry = litellm_by_uuid[uuid][0]
            existing_model_name = existing_entry.get("model_name", "")
            existing_params = existing_entry.get("litellm_params", {}) or {}

            if existing_model_name != model.model_name:
                if existing_id:
                    plan.deletes.append(existing_id)
                pricing = await self.pricing.resolve(model.model_id)
                plan.registers.append((model, pricing_to_params(pricing)))
            else:
                if existing_id:
                    plan.patches.append(
                        (
                            existing_id,
                            {
                                "model": f"hosted_vllm/{model.model_id}",
                                "api_base": model.api_base,
                            },
                        )
                    )
                plan.new_state[uuid] = model

        return plan

    async def execute(self, plan: SyncPlan) -> Tuple[int, int, int]:
        deleted, registered_ids, patched = await self.litellm.batch(
            plan.deletes, plan.registers, plan.patches
        )
        if plan.deletes:
            logger.info(f"Deleted {deleted}/{len(plan.deletes)} models")

        reg_iter = iter(registered_ids)
        for model, _ in plan.registers:
            litellm_id = next(reg_iter, None)
            if litellm_id:
                plan.new_id_map[model.uuid] = litellm_id
                plan.new_state[model.uuid] = model

        if plan.patches:
            logger.info(f"Patched {patched}/{len(plan.patches)} models")

        return deleted, len(registered_ids), patched
