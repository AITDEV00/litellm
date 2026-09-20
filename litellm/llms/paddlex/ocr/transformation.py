"""
PaddleX OCR transformation implementation.

PaddleX HPS exposes a Mistral-OCR-compatible `/v1/ocr` endpoint, so the request
and response shape is identical to Mistral's. We reuse the Mistral base config
and only:
  - widen the supported-params allow-list with the PaddleX extension fields
    (`include_native_labels`, `threshold`) that the stock Mistral DTO does not
    know about, and
  - drop the API-key requirement, since the PaddleX backend is reached over an
    internal cluster service and needs no Bearer credential.
"""

from typing import Final

from litellm.llms.mistral.ocr.transformation import MistralOCRConfig

# PaddleX extension request fields on top of the stock Mistral OCR DTO.
PADDLEX_EXTENSION_OCR_PARAMS: Final[tuple[str, ...]] = (
    "include_native_labels",
    "threshold",
)


class PaddleXOCRConfig(MistralOCRConfig):
    """
    PaddleX HPS OCR transformation configuration.

    Reuses the Mistral OCR wire format (same `document` + params JSON body and
    response shape) but additionally forwards the PaddleX-extension fields that
    are not part of the official Mistral `OCRRequest` DTO.
    """

    def get_supported_ocr_params(self, model: str) -> list:
        """
        Return the Mistral OCR params plus the PaddleX extensions.

        Super's list already covers every stock Mistral field; we append the
        PaddleX-only fields so they survive the param allow-list filter in
        `litellm.ocr.main._prepare_ocr_request`.
        """
        return [*super().get_supported_ocr_params(model=model), *PADDLEX_EXTENSION_OCR_PARAMS]

    def get_api_key_env_var(self) -> str | None:
        return None

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
        No API key is required for the in-cluster PaddleX backend. Return the
        headers unchanged.
        """
        return headers