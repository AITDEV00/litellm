"""
Tests for the Docling document conversion provider transformation.

Covers:
1. Request URL building (get_complete_url) for both `:8080` and `:8080/v1` api_base forms.
2. Request body construction (transform_document_conversion_request) mapping canonical
   sources into the Docling ``sources``/``options`` shape.
3. Response parsing (transform_document_conversion_response) into ConvertDocumentResponse.
4. Environment validation (Bearer auth + Content-Type).

These are pure-transformation tests with no live network calls.
"""

import httpx
import pytest

from litellm.llms.base_llm.document_conversion.transformation import (
    DocumentConversionSource,
)
from litellm.llms.docling.document_conversion.transformation import (
    DEFAULT_DOCLING_API_BASE,
    DOCLING_API_BASE_ENV_VAR,
    SOURCE_MIME_FIELD,
    DoclingDocumentConversionConfig,
)


class TestGetCompleteUrl:
    def test_should_append_v1_convert_source_to_bare_base(self):
        config = DoclingDocumentConversionConfig()
        url = config.get_complete_url(api_base="http://docling:8080", model="docling/PP-DocLayoutV3", optional_params={})
        assert url == "http://docling:8080/v1/convert/source"

    def test_should_not_duplicate_v1_when_base_ends_with_v1(self):
        # The controller registers api_base as "<host>:8080/v1".
        config = DoclingDocumentConversionConfig()
        url = config.get_complete_url(api_base="http://docling:8080/v1", model="m", optional_params={})
        assert url == "http://docling:8080/v1/convert/source"

    def test_should_use_default_base_when_api_base_none(self, monkeypatch):
        monkeypatch.delenv(DOCLING_API_BASE_ENV_VAR, raising=False)
        config = DoclingDocumentConversionConfig()
        url = config.get_complete_url(api_base=None, model="m", optional_params={})
        assert url == f"{DEFAULT_DOCLING_API_BASE}/v1/convert/source"

    def test_should_use_env_var_base_when_api_base_none(self, monkeypatch):
        monkeypatch.setenv(DOCLING_API_BASE_ENV_VAR, "http://docling-cluster:8090")
        config = DoclingDocumentConversionConfig()
        url = config.get_complete_url(api_base=None, model="m", optional_params={})
        assert url == "http://docling-cluster:8090/v1/convert/source"

    def test_should_prefer_explicit_api_base_over_env_var(self, monkeypatch):
        monkeypatch.setenv(DOCLING_API_BASE_ENV_VAR, "http://env-base:9999")
        config = DoclingDocumentConversionConfig()
        url = config.get_complete_url(api_base="http://explicit:8080/v1", model="m", optional_params={})
        assert url == "http://explicit:8080/v1/convert/source"

    def test_should_append_arbitrary_query_params(self):
        # Any query params captured by the proxy route are appended generically.
        config = DoclingDocumentConversionConfig()
        url = config.get_complete_url(
            api_base="http://docling:8080/v1",
            model="m",
            optional_params={"query_params": {"debug": "1"}},
        )
        assert url == "http://docling:8080/v1/convert/source?debug=1"

    def test_should_append_multiple_query_params(self):
        config = DoclingDocumentConversionConfig()
        url = config.get_complete_url(
            api_base="http://docling:8080",
            model="m",
            optional_params={"query_params": {"debug": "1", "page": "2", "format": "json"}},
        )
        assert url == "http://docling:8080/v1/convert/source?debug=1&page=2&format=json"

    def test_should_append_query_params_to_bare_base(self):
        config = DoclingDocumentConversionConfig()
        url = config.get_complete_url(
            api_base="http://docling:8080",
            model="m",
            optional_params={"query_params": {"debug": "1"}},
        )
        assert url == "http://docling:8080/v1/convert/source?debug=1"

    def test_should_omit_query_params_when_empty(self):
        config = DoclingDocumentConversionConfig()
        url = config.get_complete_url(
            api_base="http://docling:8080/v1",
            model="m",
            optional_params={"query_params": {}},
        )
        assert url == "http://docling:8080/v1/convert/source"


class TestTransformRequest:
    def test_should_build_sources_and_mime_type(self):
        config = DoclingDocumentConversionConfig()
        sources = [
            DocumentConversionSource(content="https://example.com/doc.pdf", mime_type="application/pdf"),
            DocumentConversionSource(content="data:image/png;base64,abc"),
        ]
        result = config.transform_document_conversion_request(
            model="m",
            sources=sources,
            optional_params={},
            headers={},
        )
        assert result.data == {
            "sources": [
                {"content": "https://example.com/doc.pdf", SOURCE_MIME_FIELD: "application/pdf"},
                {"content": "data:image/png;base64,abc"},
            ]
        }

    def test_should_include_to_formats_in_options(self):
        config = DoclingDocumentConversionConfig()
        sources = [DocumentConversionSource(content="https://example.com/a.pdf")]
        result = config.transform_document_conversion_request(
            model="m",
            sources=sources,
            optional_params={"to_formats": ["markdown", "json"]},
            headers={},
        )
        assert result.data["options"] == {"to_formats": ["markdown", "json"]}

    def test_should_merge_explicit_options(self):
        sources = [DocumentConversionSource(content="https://example.com/a.pdf")]
        config = DoclingDocumentConversionConfig()
        result = config.transform_document_conversion_request(
            model="m",
            sources=sources,
            optional_params={"options": {"full": True, "to_formats": ["markdown"]}},
            headers={},
        )
        assert result.data["options"] == {"full": True, "to_formats": ["markdown"]}

    def test_should_map_export_formats_alias_to_to_formats(self):
        # ``export_formats`` is a supported param; it must be wired into the
        # Docling options as ``to_formats`` (regression: it was advertised as
        # supported but silently dropped).
        config = DoclingDocumentConversionConfig()
        sources = [DocumentConversionSource(content="https://example.com/a.pdf")]
        result = config.transform_document_conversion_request(
            model="m",
            sources=sources,
            optional_params={"export_formats": ["json"]},
            headers={},
        )
        assert result.data["options"] == {"to_formats": ["json"]}

    def test_should_prefer_to_formats_over_export_formats(self):
        config = DoclingDocumentConversionConfig()
        sources = [DocumentConversionSource(content="https://example.com/a.pdf")]
        result = config.transform_document_conversion_request(
            model="m",
            sources=sources,
            optional_params={"to_formats": ["markdown"], "export_formats": ["json"]},
            headers={},
        )
        assert result.data["options"] == {"to_formats": ["markdown"]}

    def test_should_omit_options_when_empty(self):
        config = DoclingDocumentConversionConfig()
        sources = [DocumentConversionSource(content="https://example.com/a.pdf")]
        result = config.transform_document_conversion_request(
            model="m",
            sources=sources,
            optional_params={},
            headers={},
        )
        assert "options" not in result.data


class TestTransformResponse:
    def test_should_parse_docling_response(self):
        config = DoclingDocumentConversionConfig()
        raw = httpx.Response(
            200,
            json={
                "document": {"filename": "report.md"},
                "status": "success",
                "errors": [],
                "processing_time": 0.42,
            },
        )
        response = config.transform_document_conversion_response(
            model="m",
            raw_response=raw,
            logging_obj=None,
        )
        assert response.status == "success"
        assert response.processing_time == 0.42
        assert response.document == {"filename": "report.md"}
        assert response.errors == []
        assert response._additional_properties["docling_raw"]["status"] == "success"

    def test_should_parse_error_response(self):
        config = DoclingDocumentConversionConfig()
        raw = httpx.Response(
            200,
            json={
                "status": "failure",
                "errors": [
                    {
                        "component_type": "pipeline",
                        "module_name": "doctr",
                        "error_message": "boom",
                        "category": "process",
                    }
                ],
                "processing_time": 0.1,
            },
        )
        response = config.transform_document_conversion_response(
            model="m",
            raw_response=raw,
            logging_obj=None,
        )
        assert response.status == "failure"
        assert response.errors is not None
        assert response.errors[0].error_message == "boom"
        assert response.errors[0].component_type == "pipeline"

    def test_should_preserve_raw_bytes_for_debug_png_response(self):
        config = DoclingDocumentConversionConfig()
        raw = httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\nfakepng",
            headers={"content-type": "image/png"},
        )
        response = config.transform_document_conversion_response(
            model="m",
            raw_response=raw,
            logging_obj=None,
        )
        assert response._additional_properties["docling_raw"] == b"\x89PNG\r\n\x1a\nfakepng"
        assert response._additional_properties["docling_raw_content_type"] == "image/png"


class TestValidateEnvironment:
    def test_should_set_content_type_without_auth(self):
        config = DoclingDocumentConversionConfig()
        headers = config.validate_environment(headers={}, model="m", api_key=None)
        assert headers.get("Content-Type") == "application/json"
        assert "Authorization" not in headers

    def test_should_add_bearer_when_api_key_present(self):
        config = DoclingDocumentConversionConfig()
        headers = config.validate_environment(headers={}, model="m", api_key="secret")
        assert headers.get("Authorization") == "Bearer secret"


class TestSupportedParams:
    def test_should_expose_docling_params(self):
        config = DoclingDocumentConversionConfig()
        assert set(config.get_supported_document_conversion_params(model="m")) == {
            "to_formats",
            "options",
            "export_formats",
        }


class TestConvertCost:
    def test_unmapped_model_bills_zero(self):
        """An unmapped docling model (no pricing entry) must bill 0.0, not raise."""
        from litellm import completion_cost
        from litellm.llms.base_llm.document_conversion.transformation import (
            ConvertDocumentResponse,
        )

        response = ConvertDocumentResponse(
            document={"filename": "x.md"},
            status="success",
            errors=[],
            processing_time=0.42,
        )
        cost = completion_cost(
            completion_response=response,
            model="docling/PP-DocLayoutV3",
            custom_llm_provider="docling",
            call_type="aconvert",
        )
        assert cost == 0.0

    def test_mapped_model_bills_per_conversion(self, monkeypatch):
        """A docling model with cost_per_conversion configured bills per conversion."""
        from litellm import completion_cost
        from litellm.llms.base_llm.document_conversion.transformation import (
            ConvertDocumentResponse,
        )

        response = ConvertDocumentResponse(
            document={"filename": "x.md"},
            status="success",
            errors=[],
            processing_time=0.42,
        )
        monkeypatch.setattr(
            "litellm.cost_calculator._cached_get_model_info_helper",
            lambda model, custom_llm_provider: {"cost_per_conversion": 0.01},
        )
        cost = completion_cost(
            completion_response=response,
            model="docling/PP-DocLayoutV3",
            custom_llm_provider="docling",
            call_type="convert",
        )
        assert cost == pytest.approx(0.01)