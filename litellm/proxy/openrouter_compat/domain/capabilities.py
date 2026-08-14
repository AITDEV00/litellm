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

    is_moderated: bool | None = None
    supported_parameters: set[str] | None = None
    supported_voices: list[str] | None = None


class ApiCapabilities(BaseModel):
    chat_completions: bool | None = None
    completions: bool | None = None
    responses: bool | None = None
    embeddings: bool | None = None
    transcription: bool | None = None
    speech: bool | None = None
    rerank: bool | None = None
    classification: bool | None = None
    routes: set[str] = Field(default_factory=set)