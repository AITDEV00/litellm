"""Vendored OpenRouter schema subset.

The official OpenRouter Python SDK pins ``pydantic<2.13`` which conflicts with
litellm's locked ``pydantic 2.13.4`` (design §24 sanctioned vendoring fallback).
Only the mapper imports these types; discovery/domain never depend on them.

The classes mirror the current official SDK output so serialized JSON validates
against the OpenRouter contract. Keep this file in sync with the upstream SDK.
"""

from __future__ import annotations

from typing import Literal, Optional, TypeVar, TYPE_CHECKING, Union
from typing_extensions import TypeAlias, TypeAliasType

from pydantic import ConfigDict, model_serializer
from pydantic import BaseModel as PydanticBaseModel
from pydantic_core import core_schema


class BaseModel(PydanticBaseModel):
    model_config = ConfigDict(
        populate_by_name=True, arbitrary_types_allowed=True, protected_namespaces=()
    )


class Unset(BaseModel):
    @model_serializer(mode="plain")
    def serialize_model(self) -> str:
        return UNSET_SENTINEL

    def __bool__(self) -> Literal[False]:
        return False


UNSET = Unset()
UNSET_SENTINEL = "~?~unset~?~sentinel~?~"

T = TypeVar("T")
if TYPE_CHECKING:
    Nullable: TypeAlias = Union[T, None]
    OptionalNullable: TypeAlias = Union[Optional[Nullable[T]], Unset]
else:
    Nullable = TypeAliasType("Nullable", Union[T, None], type_params=(T,))
    OptionalNullable = TypeAliasType(
        "OptionalNullable", Union[Optional[Nullable[T]], Unset], type_params=(T,)
    )


class UnrecognizedStr(str):
    @classmethod
    def __get_pydantic_core_schema__(cls, _source_type, _handler) -> core_schema.CoreSchema:  # pyright: ignore[reportUnknownParameterType, reportMissingParameterType]  # pydantic dunder requires untyped handler
        def validate_lax(v: object) -> UnrecognizedStr:
            if isinstance(v, cls):
                return v
            return cls(str(v))

        return core_schema.lax_or_strict_schema(
            lax_schema=core_schema.chain_schema(
                [
                    core_schema.str_schema(),
                    core_schema.no_info_plain_validator_function(validate_lax),
                ]
            ),
            strict_schema=core_schema.none_schema(),
        )