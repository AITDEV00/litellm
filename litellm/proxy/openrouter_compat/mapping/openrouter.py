"""OpenRouter model mapper. Only this layer imports the OpenRouter schema (design)."""

from __future__ import annotations

from typing import Literal, cast

from litellm.proxy.openrouter_compat.domain.capabilities import ModelCapabilities
from litellm.proxy.openrouter_compat.domain.logical_model import AggregatedModel
from litellm.proxy.openrouter_compat.domain.limits import ModelLimits
from litellm.proxy.openrouter_compat.enrichment.litellm_metadata import (
    LiteLLMMetadataEnricher,
)
from litellm.proxy.openrouter_compat.enrichment.pricing import Pricing, PricingResolver
from litellm.proxy.openrouter_compat.openrouter_schema.base import UnrecognizedStr
from litellm.proxy.openrouter_compat.openrouter_schema.models import (
    Model,
    ModelArchitecture,
    ModelLinks,
    Parameter,
    PerRequestLimits,
    PublicPricing,
    TopProviderInfo,
)


class OpenRouterModelMapper:
    """Map canonical AggregatedModel objects into OpenRouter Model objects."""

    def __init__(
        self,
        *,
        details_base_url: str,
        canonical_namespace: str = "litellm",
        is_moderated_default: bool = False,
        pricing_resolver: PricingResolver | None = None,
    ) -> None:
        self._details_base_url = details_base_url.rstrip("/")
        self._canonical_namespace = canonical_namespace
        self._is_moderated_default = is_moderated_default
        self._pricing_resolver = pricing_resolver or PricingResolver()
        self._metadata = LiteLLMMetadataEnricher()

    def map_model(self, model: AggregatedModel) -> Model:
        enriched = self._metadata.enrich(model)
        identity = enriched.identity
        public_id = identity.logical_model_name
        slug = self._canonical_slug(public_id)
        pricing = self._pricing_resolver.resolve(enriched) or Pricing(prompt="0", completion="0")
        limits = enriched.limits
        caps = enriched.capabilities
        input_modalities, output_modalities, modality = self._map_modalities(caps)
        supported_params = cast(
            list[Parameter],
            [UnrecognizedStr(p) for p in self._metadata.supported_parameters(enriched)],
        )

        return Model(
            id=public_id,
            canonical_slug=slug,
            name=identity.display_name or identity.upstream_model_id or public_id,
            created=self._created(enriched),
            pricing=PublicPricing(prompt=pricing.prompt, completion=pricing.completion),
            context_length=limits.context_length,
            architecture=ModelArchitecture(
                modality=modality,
                input_modalities=input_modalities,
                output_modalities=output_modalities,
            ),
            top_provider=TopProviderInfo(
                is_moderated=self._is_moderated_default,
                context_length=limits.context_length,
                max_completion_tokens=limits.max_completion_tokens,
            ),
            per_request_limits=self._per_request_limits(limits),
            supported_parameters=supported_params,
            default_parameters=None,
            supported_voices=None,
            links=ModelLinks(details=self._details_url(slug)),
            hugging_face_id=identity.hugging_face_id,
        )

    def map_placeholder(self, logical_model_name: str) -> Model:
        """Emit a conformant Model for a model that failed discovery.

        Keeps the OpenRouter shape so the ``data`` array stays uniform, but
        uses honest zeros/None for unknown semantics and an informative
        description instead of silently dropping the model.
        """
        slug = self._canonical_slug(logical_model_name)
        return Model(
            id=logical_model_name,
            canonical_slug=slug,
            name=logical_model_name,
            created=0,
            pricing=PublicPricing(prompt="0", completion="0"),
            context_length=0,
            architecture=ModelArchitecture(
                modality=None,
                input_modalities=[],
                output_modalities=[],
            ),
            top_provider=TopProviderInfo(
                is_moderated=self._is_moderated_default,
                context_length=None,
                max_completion_tokens=None,
            ),
            per_request_limits=None,
            supported_parameters=[],
            default_parameters=None,
            supported_voices=None,
            links=ModelLinks(details=self._details_url(slug)),
            hugging_face_id=None,
            description=(
                f"{logical_model_name} is not properly configured or deployed to "
                "follow OpenRouter / OpenAI conventions; discovery produced no "
                "usable endpoint."
            ),
        )

    def _canonical_slug(self, public_id: str) -> str:
        if "/" in public_id:
            return public_id
        return f"{self._canonical_namespace}/{public_id}"

    def _details_url(self, slug: str) -> str:
        return f"{self._details_base_url}/api/v1/models/{slug}/endpoints"

    @staticmethod
    def _map_modalities(
        caps: ModelCapabilities,
    ) -> tuple[
        list[UnrecognizedStr | Literal["text", "image", "file", "audio", "video"]],
        list[
            UnrecognizedStr
            | Literal["text", "image", "embeddings", "audio", "video", "rerank", "speech", "transcription"]
        ],
        str | None,
    ]:
        input_mods: list[UnrecognizedStr | Literal["text", "image", "file", "audio", "video"]] = (
            [UnrecognizedStr(m) for m in sorted(caps.input_modalities)] if caps.input_modalities else []
        )
        output_mods: list[
            UnrecognizedStr
            | Literal["text", "image", "embeddings", "audio", "video", "rerank", "speech", "transcription"]
        ] = [UnrecognizedStr(m) for m in sorted(caps.output_modalities)] if caps.output_modalities else []
        has_text = any(str(m) == "text" for m in input_mods)
        modality = "text" if has_text else None
        return input_mods, output_mods, modality

    @staticmethod
    def _created(enriched: AggregatedModel) -> int:
        return enriched.identity.created or 0

    @staticmethod
    def _per_request_limits(limits: ModelLimits) -> PerRequestLimits | None:
        if limits.max_input_tokens is None and limits.max_completion_tokens is None:
            return None
        return PerRequestLimits(
            prompt_tokens=float(limits.max_input_tokens or 0),
            completion_tokens=float(limits.max_completion_tokens or 0),
        )
