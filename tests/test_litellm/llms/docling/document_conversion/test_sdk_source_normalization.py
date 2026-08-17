"""
Tests for the document conversion SDK helpers (source normalization).

These verify the user-facing ``sources`` argument is normalized into a list of
``DocumentConversionSource`` exactly as the Docling provider expects.
"""

import pytest

from litellm.document_conversion.main import _convert_sources
from litellm.llms.base_llm.document_conversion.transformation import (
    DocumentConversionSource,
)


class TestConvertSources:
    def test_should_accept_bare_string(self):
        sources = _convert_sources("https://example.com/a.pdf")
        assert len(sources) == 1
        assert sources[0].content == "https://example.com/a.pdf"
        assert sources[0].mime_type is None

    def test_should_accept_single_dict(self):
        sources = _convert_sources({"content": "https://example.com/a.pdf", "mime_type": "application/pdf"})
        assert len(sources) == 1
        assert sources[0].mime_type == "application/pdf"

    def test_should_accept_list_of_mixed(self):
        sources = _convert_sources(
            [
                {"content": "https://example.com/a.pdf", "mime_type": "application/pdf"},
                "data:image/png;base64,abc",
            ]
        )
        assert len(sources) == 2
        assert sources[1].content == "data:image/png;base64,abc"

    def test_should_accept_existing_instances(self):
        src = DocumentConversionSource(content="https://example.com/a.pdf")
        sources = _convert_sources([src])
        assert sources == [src]

    def test_should_reject_empty_list(self):
        with pytest.raises(ValueError, match="must not be empty"):
            _convert_sources([])

    def test_should_reject_invalid_source_type(self):
        with pytest.raises(ValueError, match="Invalid source"):
            _convert_sources([12345])

    def test_should_reject_non_list_dict_str(self):
        with pytest.raises(ValueError, match="sources must be a list, dict, or str"):
            _convert_sources(12345)