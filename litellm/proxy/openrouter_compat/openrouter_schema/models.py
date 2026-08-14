"""OpenRouter public contract types, vendored subset (design §24 fallback).

Only the mapper imports these. Serialized output matches the official
OpenRouter Python SDK contract.
"""

from __future__ import annotations

from typing import Callable, List, Literal, Optional, Union

from pydantic import model_serializer

from litellm.proxy.openrouter_compat.openrouter_schema.base import (
    BaseModel,
    Nullable,
    OptionalNullable,
    UNSET,
    UNSET_SENTINEL,
    UnrecognizedStr,
)

# Handler injected by pydantic's model_serializer(mode="wrap"); returns the
# base dict to which optional/nullable fields are applied.
SerializerHandler = Callable[[object], dict[str, object]]


def _serialize_wrap(
    self: BaseModel,
    handler: SerializerHandler,
    *,
    optional_fields: set[str],
    nullable_fields: set[str],
) -> dict[str, object]:
    serialized = handler(self)
    m: dict[str, object] = {}
    for n, f in type(self).model_fields.items():
        k = f.alias or n
        val = serialized.get(k, serialized.get(n))
        is_nullable_and_explicitly_set = k in nullable_fields and bool(
            self.__pydantic_fields_set__.intersection({n})
        )
        if val != UNSET_SENTINEL:
            if val is not None or k not in optional_fields or is_nullable_and_explicitly_set:
                m[k] = val
    return m

__all__ = [
    "Parameter",
    "ModelArchitecture",
    "PublicPricing",
    "TopProviderInfo",
    "ModelLinks",
    "DefaultParameters",
    "PerRequestLimits",
    "Model",
]


Parameter = Union[
    Literal[
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "top_a",
        "frequency_penalty",
        "presence_penalty",
        "repetition_penalty",
        "max_tokens",
        "max_completion_tokens",
        "logit_bias",
        "logprobs",
        "top_logprobs",
        "prediction",
        "seed",
        "response_format",
        "structured_outputs",
        "stop",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "include_reasoning",
        "reasoning",
        "reasoning_effort",
        "web_search_options",
        "verbosity",
    ],
    UnrecognizedStr,
]


class ModelArchitecture(BaseModel):
    input_modalities: List[
        Union[Literal["text", "image", "file", "audio", "video"], UnrecognizedStr]
    ]
    modality: Nullable[str]
    output_modalities: List[
        Union[
            Literal[
                "text",
                "image",
                "embeddings",
                "audio",
                "video",
                "rerank",
                "speech",
                "transcription",
            ],
            UnrecognizedStr,
        ]
    ]
    instruct_type: OptionalNullable[
        Union[
            Literal[
                "none",
                "airoboros",
                "alpaca",
                "alpaca-modif",
                "chatml",
                "claude",
                "code-llama",
                "gemma",
                "llama2",
                "llama3",
                "mistral",
                "nemotron",
                "neural",
                "openchat",
                "phi3",
                "rwkv",
                "vicuna",
                "zephyr",
                "deepseek-r1",
                "deepseek-v3.1",
                "qwq",
                "qwen3",
            ],
            UnrecognizedStr,
        ]
    ] = UNSET
    tokenizer: OptionalNullable[
        Union[
            Literal[
                "Router",
                "Media",
                "Other",
                "GPT",
                "Claude",
                "Gemini",
                "Gemma",
                "Grok",
                "Cohere",
                "Nova",
                "Qwen",
                "Yi",
                "DeepSeek",
                "Mistral",
                "Llama2",
                "Llama3",
                "Llama4",
                "PaLM",
                "RWKV",
                "Qwen3",
            ],
            UnrecognizedStr,
        ]
    ] = UNSET

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerHandler) -> dict[str, object]:
        return _serialize_wrap(
            self,
            handler,
            optional_fields={"instruct_type", "tokenizer"},
            nullable_fields={"instruct_type", "modality"},
        )


class PublicPricing(BaseModel):
    completion: str
    prompt: str
    audio: Optional[str] = None
    audio_output: Optional[str] = None
    discount: Optional[float] = None
    image: Optional[str] = None
    image_output: Optional[str] = None
    image_token: Optional[str] = None
    input_audio_cache: Optional[str] = None
    input_cache_read: Optional[str] = None
    input_cache_write: Optional[str] = None
    input_cache_write_1h: Optional[str] = None
    internal_reasoning: Optional[str] = None
    request: Optional[str] = None
    web_search: Optional[str] = None

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerHandler) -> dict[str, object]:
        return _serialize_wrap(
            self,
            handler,
            optional_fields={
                "audio",
                "audio_output",
                "discount",
                "image",
                "image_output",
                "image_token",
                "input_audio_cache",
                "input_cache_read",
                "input_cache_write",
                "input_cache_write_1h",
                "internal_reasoning",
                "request",
                "web_search",
            },
            nullable_fields=set(),
        )


class TopProviderInfo(BaseModel):
    is_moderated: bool
    context_length: OptionalNullable[int] = UNSET
    max_completion_tokens: OptionalNullable[int] = UNSET

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerHandler) -> dict[str, object]:
        return _serialize_wrap(
            self,
            handler,
            optional_fields={"context_length", "max_completion_tokens"},
            nullable_fields={"context_length", "max_completion_tokens"},
        )


class ModelLinks(BaseModel):
    details: str


class DefaultParameters(BaseModel):
    frequency_penalty: OptionalNullable[float] = UNSET
    presence_penalty: OptionalNullable[float] = UNSET
    repetition_penalty: OptionalNullable[float] = UNSET
    temperature: OptionalNullable[float] = UNSET
    top_k: OptionalNullable[int] = UNSET
    top_p: OptionalNullable[float] = UNSET

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerHandler) -> dict[str, object]:
        optional = {
            "frequency_penalty",
            "presence_penalty",
            "repetition_penalty",
            "temperature",
            "top_k",
            "top_p",
        }
        return _serialize_wrap(
            self, handler, optional_fields=optional, nullable_fields=set(optional)
        )


class PerRequestLimits(BaseModel):
    completion_tokens: float
    prompt_tokens: float


class Model(BaseModel):
    architecture: ModelArchitecture
    canonical_slug: str
    context_length: Nullable[int]
    created: int
    default_parameters: Nullable[DefaultParameters]
    id: str
    links: ModelLinks
    name: str
    per_request_limits: Nullable[PerRequestLimits]
    pricing: PublicPricing
    supported_parameters: List[Parameter]
    supported_voices: Nullable[List[str]]
    top_provider: TopProviderInfo
    description: Optional[str] = None
    hugging_face_id: OptionalNullable[str] = UNSET

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerHandler) -> dict[str, object]:
        return _serialize_wrap(
            self,
            handler,
            optional_fields={"description", "hugging_face_id"},
            nullable_fields={
                "context_length",
                "default_parameters",
                "hugging_face_id",
                "per_request_limits",
                "supported_voices",
            },
        )