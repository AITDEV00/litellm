"""Model and API capability facts. Tri-state semantics: True/False/None (unknown)."""

from pydantic import BaseModel, Field


class ModelCapabilities(BaseModel):
    input_modalities: set[str] | None = None
    output_modalities: set[str] | None = None

    tool_calling: bool | None = None
    parallel_tool_calling: bool | None = None
    reasoning: bool | None = None
    structured_outputs: bool | None = None
    logprobs: bool | None = None
    embeddings: bool | None = None
    rerank: bool | None = None


class ApiCapabilities(BaseModel):
    chat_completions: bool | None = None
    completions: bool | None = None
    responses: bool | None = None
    embeddings: bool | None = None
    transcription: bool | None = None
    speech: bool | None = None
    rerank: bool | None = None
    classification: bool | None = None
    image_generation: bool | None = None
    image_edits: bool | None = None
    video_generation: bool | None = None
    voices: bool | None = None
    moderation: bool | None = None
    batches: bool | None = None
    files: bool | None = None
    routes: set[str] = Field(default_factory=set)