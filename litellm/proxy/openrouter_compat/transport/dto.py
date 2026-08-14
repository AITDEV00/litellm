"""Permissive upstream runtime DTOs. Unknown future fields must not fail."""

from pydantic import BaseModel, ConfigDict, Field


class UpstreamDTO(BaseModel):
    model_config = ConfigDict(extra="allow")


class OpenAICompatibleModelCard(UpstreamDTO):
    id: str
    object: str | None = None
    created: int | None = None
    owned_by: str | None = None
    root: str | None = None
    parent: str | None = None


class RuntimeModelCard(OpenAICompatibleModelCard):
    max_model_len: int | None = None


class VLLMModelCard(RuntimeModelCard):
    permission: list[dict[str, object]] = Field(default_factory=list)


class SGLangModelCard(RuntimeModelCard):
    pass


class SGLangModelInfo(UpstreamDTO):
    model_path: str | None = None
    tokenizer_path: str | None = None
    is_generation: bool | None = None
    preferred_sampling_params: dict[str, object] | None = None
    weight_version: str | None = None
    has_image_understanding: bool | None = None
    has_audio_understanding: bool | None = None
    model_type: str | None = None
    architectures: list[str] | None = None