"""Group discovered deployments into logical aggregated models (design §20)."""

from __future__ import annotations

from collections import defaultdict

from litellm.proxy.openrouter_compat.aggregation.policies import (
    ContextAggregationPolicy,
    aggregate_bool_capability,
    aggregate_limits,
)
from litellm.proxy.openrouter_compat.domain.architecture import ModelArchitecture
from litellm.proxy.openrouter_compat.domain.capabilities import ModelCapabilities
from litellm.proxy.openrouter_compat.domain.deployment import DiscoveredDeploymentModel
from litellm.proxy.openrouter_compat.domain.identity import ModelIdentity
from litellm.proxy.openrouter_compat.domain.logical_model import AggregatedModel


class ModelAggregator:
    def __init__(
        self,
        context_policy: ContextAggregationPolicy = ContextAggregationPolicy.GUARANTEED_MIN,
        conservative: bool = True,
    ) -> None:
        self._context_policy = context_policy
        self._conservative = conservative

    def aggregate_all(
        self, discoveries: list[DiscoveredDeploymentModel]
    ) -> list[AggregatedModel]:
        by_name: dict[str, list[DiscoveredDeploymentModel]] = defaultdict(list)
        for model in discoveries:
            by_name[model.identity.logical_model_name].append(model)
        return [
            self.aggregate(logical_model_name, deployments)
            for logical_model_name, deployments in by_name.items()
        ]

    def aggregate(
        self,
        logical_model_name: str,
        deployments: list[DiscoveredDeploymentModel],
    ) -> AggregatedModel:
        limits = aggregate_limits(
            [d.limits for d in deployments], self._context_policy
        )
        capabilities = self._aggregate_capabilities(deployments)
        architecture = self._aggregate_architecture(deployments)
        identity = self._aggregate_identity(logical_model_name, deployments)
        return AggregatedModel(
            logical_model_name=logical_model_name,
            deployments=deployments,
            identity=identity,
            limits=limits,
            architecture=architecture,
            capabilities=capabilities,
            pricing=None,
        )

    def _aggregate_capabilities(
        self, deployments: list[DiscoveredDeploymentModel]
    ) -> ModelCapabilities:
        def bools(attr: str) -> list[bool | None]:
            return [getattr(d.capabilities, attr) for d in deployments]

        input_modalities = self._aggregate_modalities(
            [d.capabilities.input_modalities for d in deployments]
        )
        output_modalities = self._aggregate_modalities(
            [d.capabilities.output_modalities for d in deployments]
        )
        return ModelCapabilities(
            input_modalities=input_modalities,
            output_modalities=output_modalities,
            tool_calling=aggregate_bool_capability(
                bools("tool_calling"), conservative=self._conservative
            ),
            parallel_tool_calling=aggregate_bool_capability(
                bools("parallel_tool_calling"), conservative=self._conservative
            ),
            reasoning=aggregate_bool_capability(
                bools("reasoning"), conservative=self._conservative
            ),
            structured_outputs=aggregate_bool_capability(
                bools("structured_outputs"), conservative=self._conservative
            ),
            logprobs=aggregate_bool_capability(
                bools("logprobs"), conservative=self._conservative
            ),
            embeddings=aggregate_bool_capability(
                bools("embeddings"), conservative=self._conservative
            ),
            rerank=aggregate_bool_capability(
                bools("rerank"), conservative=self._conservative
            ),
        )

    @staticmethod
    def _aggregate_modalities(sets: list[set[str] | None]) -> set[str] | None:
        """Guaranteed modalities: intersection across all known deployments."""
        known = [s for s in sets if s is not None]
        if not known:
            return None
        intersection = known[0]
        for s in known[1:]:
            intersection = intersection & s
        return intersection

    @staticmethod
    def _aggregate_architecture(
        deployments: list[DiscoveredDeploymentModel],
    ) -> ModelArchitecture:
        model_types = {
            d.architecture.model_type for d in deployments if d.architecture.model_type
        }
        archs: set[str] = set()
        for d in deployments:
            archs.update(d.architecture.architectures)
        return ModelArchitecture(
            model_type=next(iter(model_types), None),
            architectures=sorted(archs),
            tokenizer=next(
                (d.architecture.tokenizer for d in deployments if d.architecture.tokenizer),
                None,
            ),
            instruct_type=next(
                (
                    d.architecture.instruct_type
                    for d in deployments
                    if d.architecture.instruct_type
                ),
                None,
            ),
        )

    @staticmethod
    def _aggregate_identity(
        logical_model_name: str, deployments: list[DiscoveredDeploymentModel]
    ) -> ModelIdentity:
        first = deployments[0].identity
        display_name = first.display_name or first.upstream_model_id or logical_model_name
        created = next(
            (d.identity.created for d in deployments if d.identity.created), None
        )
        return ModelIdentity(
            logical_model_name=logical_model_name,
            upstream_model_id=first.upstream_model_id,
            root=first.root,
            parent=first.parent,
            display_name=display_name,
            canonical_id=first.canonical_id,
            hugging_face_id=first.hugging_face_id,
            created=created,
        )