"""
Base document_conversion transformation configuration.

This is the shared interface for the ``document_conversion`` capability:
turning a document source (HTTP URL or base64 data URI) into a converted
output (markdown / other export formats) via a document-processing provider
such as Docling.

The canonical response shape follows the Docling ``ConvertDocumentResponse``
contract, which is also what cloud document-processing APIs converge on:

- ``document``: the export (e.g. markdown) with its filename / content
- ``status``: success / failure
- ``errors``: per-item error details
- ``processing_time``: seconds spent

Providers implement a ``BaseDocumentConversionConfig`` and map their own native
request/response shapes into/out of this canonical contract.
"""

from typing import TYPE_CHECKING, Any

import httpx
from pydantic import Field, PrivateAttr

from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.types.llms.base import LiteLLMPydanticObjectBase

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj
else:
    LiteLLMLoggingObj = Any


class ConversionErrorItem(LiteLLMPydanticObjectBase):
    """A single error item attached to a document conversion response."""

    component_type: str | None = None
    module_name: str | None = None
    error_message: str | None = None
    category: str | None = None

    model_config = {"extra": "allow"}


class ConvertDocumentResponse(LiteLLMPydanticObjectBase):
    """
    Canonical document conversion response.

    Mirrors the Docling ``ConvertDocumentResponse`` so that any provider maps
    into the same shape.
    """

    document: dict[str, Any] | None = None
    status: str | None = None
    errors: list[ConversionErrorItem] | None = None
    processing_time: float | None = None

    # Provider-specific raw payload, preserved for debugging / pass-through.
    _hidden_params: dict[str, Any] = PrivateAttr(default_factory=dict)
    _additional_properties: dict[str, Any] = PrivateAttr(default_factory=dict)

    def get_raw_response_payload(self) -> Any:
        """
        Return the provider's raw response payload.

        Providers may stash the raw upstream response here (parsed JSON for a
        normal conversion, or raw bytes + content type when Docling returns a
        binary ``debug`` payload). Callers such as the proxy route use this to
        surface raw bytes verbatim instead of touching the private attribute.
        """
        return self._additional_properties.get("docling_raw")

    def get_raw_response_content_type(self) -> str | None:
        """
        Return the content type of the raw upstream response, if it was a
        binary payload (e.g. ``image/png`` for a Docling ``debug`` render).
        """
        value = self._additional_properties.get("docling_raw_content_type")
        return value if isinstance(value, str) else None

    model_config = {"extra": "allow"}


class DocumentConversionRequestData(LiteLLMPydanticObjectBase):
    """Request data produced by a provider's transform_document_conversion_request."""

    data: dict | bytes | None = None
    files: dict[str, Any] | None = None


class DocumentConversionSource(LiteLLMPydanticObjectBase):
    """
    A single source document. Either an HTTP(S) URL or an inline base64 data URI.
    """

    content: str
    mime_type: str | None = Field(
        default=None,
        # The proxy route and the Docling upstream contract use camelCase
        # ``mimeType``; accept it as an alias so it isn't silently dropped.
        alias="mimeType",
    )

    model_config = {"extra": "allow", "populate_by_name": True}


class BaseDocumentConversionConfig:
    """
    Base configuration for document conversion transformations.
    Handles provider-agnostic document-conversion operations.

    Subclasses (one per provider) implement the abstract hooks:
    - ``get_complete_url``
    - ``transform_document_conversion_request``
    - ``transform_document_conversion_response``
    """

    def __init__(self) -> None:
        pass

    def get_supported_document_conversion_params(self, model: str) -> list[str]:
        """
        Get supported document-conversion params for this provider.

        Override in provider-specific implementations.
        """
        return []

    def get_api_key_env_var(self) -> str | None:
        """
        Return the provider-specific API key environment variable name, if any.
        """
        return None

    def map_document_conversion_params(
        self,
        non_default_params: dict[str, Any],
        optional_params: dict[str, Any],
        model: str,
    ) -> dict[str, Any]:
        """Map document-conversion params to provider-specific params."""
        return dict(optional_params)

    def validate_environment(
        self,
        headers: dict,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        litellm_params: dict | None = None,
        **kwargs,
    ) -> dict:
        """
        Validate environment and return headers.

        Override in provider-specific implementations.
        """
        return headers

    def get_complete_url(
        self,
        api_base: str | None,
        model: str,
        optional_params: dict,
        litellm_params: dict | None = None,
        **kwargs,
    ) -> str:
        """
        Get the complete URL for the document-conversion endpoint.

        Override in provider-specific implementations.
        """
        raise NotImplementedError("get_complete_url must be implemented by provider")

    def transform_document_conversion_request(
        self,
        model: str,
        sources: list[DocumentConversionSource],
        optional_params: dict,
        headers: dict,
        **kwargs,
    ) -> DocumentConversionRequestData:
        """
        Transform the document-conversion request to provider-specific format.

        Override in provider-specific implementations.

        Args:
            model: Model name
            sources: List of sources to convert
            optional_params: Optional parameters
            headers: Request headers

        Returns:
            DocumentConversionRequestData with data and files fields
        """
        raise NotImplementedError("transform_document_conversion_request must be implemented by provider")

    def transform_document_conversion_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: LiteLLMLoggingObj,
        **kwargs,
    ) -> ConvertDocumentResponse:
        """
        Transform provider-specific document-conversion response to canonical format.

        Override in provider-specific implementations.
        """
        raise NotImplementedError("transform_document_conversion_response must be implemented by provider")

    def get_error_class(
        self,
        error_message: str,
        status_code: int,
        headers: dict,
    ) -> Exception:
        """Get appropriate error class for the provider."""
        return BaseLLMException(
            status_code=status_code,
            message=error_message,
            headers=headers,
        )