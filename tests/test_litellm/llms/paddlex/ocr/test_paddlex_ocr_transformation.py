"""
Unit tests for PaddleXOCRConfig transformation.

PaddleX HPS exposes a Mistral-OCR-compatible `/v1/ocr` endpoint. The config
must forward the stock Mistral params AND the PaddleX-extension fields
(`include_native_labels`, `threshold`) that the stock Mistral allow-list drops.
No real API calls are made — all tests are fully mocked/local.
"""

import pytest

from litellm.llms.mistral.ocr.transformation import MistralOCRConfig
from litellm.llms.paddlex.ocr.transformation import (
    PADDLEX_EXTENSION_OCR_PARAMS,
    PaddleXOCRConfig,
)

MODEL = "PP-DocLayoutV3"


@pytest.fixture
def config() -> PaddleXOCRConfig:
    return PaddleXOCRConfig()


class TestGetSupportedOcrParams:
    def test_inherits_all_mistral_params(self, config: PaddleXOCRConfig) -> None:
        """PaddleX must keep every stock Mistral OCR param."""
        mistral_supported: set[str] = set(MistralOCRConfig().get_supported_ocr_params(model=MODEL))
        supported: set[str] = set(config.get_supported_ocr_params(model=MODEL))
        assert mistral_supported <= supported

    def test_extension_params_in_supported_list(self, config: PaddleXOCRConfig) -> None:
        """The PaddleX-extension fields must survive the param allow-list."""
        supported = config.get_supported_ocr_params(model=MODEL)
        for param in PADDLEX_EXTENSION_OCR_PARAMS:
            assert param in supported


class TestMapOcrParams:
    @pytest.mark.parametrize(
        "param_name,param_value",
        [
            ("include_native_labels", True),
            ("include_native_labels", False),
            ("threshold", 0.3),
            ("threshold", 0.99),
        ],
    )
    def test_extension_params_passed_through(
        self, config: PaddleXOCRConfig, param_name: str, param_value
    ) -> None:
        """Extension params must survive map_ocr_params, which is the gateway's
        drop-point for unknown OCR fields."""
        result = config.map_ocr_params(
            non_default_params={param_name: param_value},
            optional_params={},
            model=MODEL,
        )
        assert result == {param_name: param_value}

    def test_mistral_params_still_forwarded(self, config: PaddleXOCRConfig) -> None:
        """Stock Mistral fields (e.g. confidence_scores_granularity) must still
        be forwarded alongside the extensions."""
        result = config.map_ocr_params(
            non_default_params={
                "confidence_scores_granularity": "block",
                "include_native_labels": True,
                "threshold": 0.3,
            },
            optional_params={},
            model=MODEL,
        )
        assert result == {
            "confidence_scores_granularity": "block",
            "include_native_labels": True,
            "threshold": 0.3,
        }

    def test_unknown_param_still_dropped(self, config: PaddleXOCRConfig) -> None:
        """Params outside both the Mistral and PaddleX sets must still be dropped."""
        result = config.map_ocr_params(
            non_default_params={"include_native_labels": True, "bogus_param": "x"},
            optional_params={},
            model=MODEL,
        )
        assert "include_native_labels" in result
        assert "bogus_param" not in result


class TestTransformOcrRequest:
    SAMPLE_DOCUMENT = {
        "type": "document_url",
        "document_url": "https://example.com/doc.pdf",
    }

    @pytest.mark.parametrize(
        "param_name,param_value",
        [
            ("include_native_labels", True),
            ("threshold", 0.3),
            ("confidence_scores_granularity", "block"),
        ],
    )
    def test_param_included_in_request_body(
        self, config: PaddleXOCRConfig, param_name: str, param_value
    ) -> None:
        """Extension and stock params must reach the final upstream body."""
        result = config.transform_ocr_request(
            model=MODEL,
            document=self.SAMPLE_DOCUMENT,
            optional_params={param_name: param_value},
            headers={},
        )
        assert result.data[param_name] == param_value
        assert result.data["model"] == MODEL
        assert result.data["document"] == self.SAMPLE_DOCUMENT


class TestValidateEnvironment:
    def test_no_api_key_required(self, config: PaddleXOCRConfig) -> None:
        """The in-cluster PaddleX backend needs no Bearer credential; headers
        must pass through unchanged."""
        headers = {"X-Custom": "value"}
        assert config.validate_environment(headers=headers, model=MODEL) == headers

    def test_no_api_key_env_var(self, config: PaddleXOCRConfig) -> None:
        assert config.get_api_key_env_var() is None