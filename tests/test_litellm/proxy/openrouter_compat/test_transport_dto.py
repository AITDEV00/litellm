"""Tests for permissive upstream runtime DTOs (design §6, §37.1).

Regression focus: known fields parse; unknown future fields do not fail;
missing optional fields do not fail.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from litellm.proxy.openrouter_compat.transport.dto import (
    OpenAICompatibleModelCard,
    RuntimeModelCard,
    SGLangModelInfo,
    UpstreamDTO,
)

SGLANG_V0516_MODELS_FIXTURE = {
    "object": "list",
    "data": [
        {
            "id": "deepseek-v4",
            "object": "model",
            "created": 1720000000,
            "owned_by": "sglang",
            "root": "/models/deepseek-v4",
            "parent": None,
            "max_model_len": 262144,
        }
    ],
}

VLLM_V0240_MODELS_FIXTURE = {
    "object": "list",
    "data": [
        {
            "id": "qwen3.5-122b",
            "object": "model",
            "created": 1720000001,
            "owned_by": "vllm",
            "root": "qwen3.5-122b",
            "parent": None,
            "max_model_len": 131072,
            "permission": [{"id": "perm-1"}],
        }
    ],
}


def test_runtime_model_card_parses_known_fields():
    card = RuntimeModelCard.model_validate(VLLM_V0240_MODELS_FIXTURE["data"][0])
    assert card.id == "qwen3.5-122b"
    assert card.max_model_len == 131072
    assert card.root == "qwen3.5-122b"
    assert card.created == 1720000001


def test_upstream_dto_allows_unknown_future_fields():
    card = RuntimeModelCard.model_validate(
        {
            "id": "x",
            "future_field": {"nested": [1, 2, 3]},
            "another_new": "ignored",
        }
    )
    assert card.id == "x"
    assert card.max_model_len is None


def test_openai_card_missing_optional_fields_ok():
    card = OpenAICompatibleModelCard.model_validate({"id": "bare"})
    assert card.object is None
    assert card.created is None
    assert card.owned_by is None


def test_runtime_card_requires_id():
    with pytest.raises(ValidationError):
        RuntimeModelCard.model_validate({"max_model_len": 100})


def test_sglang_model_info_parses_rich_fields():
    info = SGLangModelInfo.model_validate(
        {
            "model_path": "/models/deepseek-v4",
            "tokenizer_path": "/tok/tokenizer",
            "is_generation": True,
            "preferred_sampling_params": {"temperature": 0.6},
            "weight_version": "1.0",
            "has_image_understanding": True,
            "has_audio_understanding": False,
            "model_type": "deepseek_v4",
            "architectures": ["DeepseekV4ForCausalLM"],
        }
    )
    assert info.model_type == "deepseek_v4"
    assert info.architectures == ["DeepseekV4ForCausalLM"]
    assert info.has_image_understanding is True
    assert info.is_generation is True


def test_sglang_model_info_allows_unknown_fields():
    info = SGLangModelInfo.model_validate({"model_type": "deepseek_v4", "unknown_new_field": {"a": 1}})
    assert info.model_type == "deepseek_v4"


def test_sglang_model_info_missing_fields_are_none():
    info = SGLangModelInfo.model_validate({})
    assert info.is_generation is None
    assert info.has_image_understanding is None
    assert info.architectures is None


def test_upstream_dto_extra_allow_on_base():
    dto = UpstreamDTO.model_validate({"anything": 1, "else": "kept"})
    assert dto.anything == 1  # type: ignore[attr-defined]
