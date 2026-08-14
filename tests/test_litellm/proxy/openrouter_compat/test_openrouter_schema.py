"""Tests for the OpenRouter public-contract schema (design §24).

Regression focus: serialized output matches the official OpenRouter contract.
No UNSET sentinel may leak into JSON, and unset optional/nullable fields must
not appear as null. Types must resolve to the official installed SDK.
"""

from __future__ import annotations

import openrouter.components.model as sdk_model
import openrouter.types.basemodel as sdk_base

from litellm.proxy.openrouter_compat.openrouter_schema.base import (
    UNSET,
    UNSET_SENTINEL,
    UnrecognizedStr,
)
from litellm.proxy.openrouter_compat.openrouter_schema.models import (
    DefaultParameters,
    Model,
    ModelArchitecture,
    ModelLinks,
    PerRequestLimits,
    PublicPricing,
    TopProviderInfo,
)


def test_public_pricing_omits_optional_none_fields():
    pricing = PublicPricing(prompt="0.30", completion="1.20")
    data = pricing.model_dump()
    assert data["prompt"] == "0.30"
    assert data["completion"] == "1.20"
    # Optional fields with None value are omitted entirely.
    assert "audio" not in data
    assert "discount" not in data
    assert UNSET_SENTINEL not in pricing.model_dump_json()


def test_default_parameters_unset_serializes_away():
    params = DefaultParameters()
    data = params.model_dump()
    assert data == {}


def test_top_provider_info_omits_unset_optional():
    info = TopProviderInfo(is_moderated=False)
    data = info.model_dump()
    assert data == {"is_moderated": False}
    assert "context_length" not in data


def test_top_provider_info_keeps_explicit_null():
    info = TopProviderInfo(is_moderated=False, context_length=None)
    data = info.model_dump()
    assert data["context_length"] is None


def test_model_architecture_with_unrecognized_modalities():
    arch = ModelArchitecture(
        input_modalities=[UnrecognizedStr("video"), UnrecognizedStr("text")],
        output_modalities=[UnrecognizedStr("text")],
        modality="text",
    )
    data = arch.model_dump()
    assert data["input_modalities"] == ["video", "text"]
    assert data["modality"] == "text"
    # instruct_type and tokenizer unset -> omitted
    assert "instruct_type" not in data
    assert "tokenizer" not in data


def test_full_model_serializes_without_sentinel_or_null_leak():
    model = Model(
        id="litellm/gpt-x",
        canonical_slug="litellm/gpt-x",
        name="GPT-X",
        created=1710000000,
        context_length=131072,
        pricing=PublicPricing(prompt="0", completion="0"),
        architecture=ModelArchitecture(
            input_modalities=[UnrecognizedStr("text")],
            output_modalities=[UnrecognizedStr("text")],
            modality="text",
        ),
        top_provider=TopProviderInfo(is_moderated=False),
        per_request_limits=PerRequestLimits(prompt_tokens=8192.0, completion_tokens=4096.0),
        supported_parameters=["temperature"],
        supported_voices=None,
        default_parameters=None,
        links=ModelLinks(details="https://proxy/api/v1/models/litellm/gpt/endpoints"),
    )
    data = model.model_dump()
    # Explicitly-set nullable fields serialize as null (matching the SDK);
    # never-constructed optional fields are omitted. No sentinel leaks.
    assert data["supported_voices"] is None
    assert data["default_parameters"] is None
    assert data["per_request_limits"] == {
        "prompt_tokens": 8192.0,
        "completion_tokens": 4096.0,
    }
    # instruct_type/tokenizer not set -> omitted from architecture.
    assert "instruct_type" not in data["architecture"]
    assert "tokenizer" not in data["architecture"]
    assert UNSET_SENTINEL not in model.model_dump_json()


def test_facade_exports_real_sdk_types():
    # Guard: the facade must resolve to the official installed SDK, not a
    # hand-maintained vendored copy (design §24).
    assert Model is sdk_model.Model
    assert ModelArchitecture is sdk_model.ModelArchitecture
    assert UnrecognizedStr is sdk_base.UnrecognizedStr
