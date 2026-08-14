"""Permissive upstream runtime DTOs. Unknown future fields must not fail."""

from pydantic import BaseModel, ConfigDict


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


class SGLangModelInfo(UpstreamDTO):
    is_generation: bool | None = None
    has_image_understanding: bool | None = None
    has_audio_understanding: bool | None = None
    model_type: str | None = None
    architectures: list[str] | None = None