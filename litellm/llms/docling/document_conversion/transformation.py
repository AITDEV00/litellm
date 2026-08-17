"""
Docling document conversion transformation implementation.

Docling (docling-serve / PaddleOCR / PaddleX HPS) exposes a Docling-compatible
HTTP API. The upstream ``ConvertSourcesRequest`` models each source as a
discriminated union on ``kind``:

    POST /v1/convert/source
    Content-Type: application/json
    {
        "sources": [
            {"kind": "http", "url": "https://example.com/doc.pdf"},
            {"kind": "file", "base64_string": "...", "filename": "report.png"}
        ],
        "options": {"from_formats": ["image"], "to_formats": ["markdown"]}
    }

``kind: "http"`` sources carry a ``url`` (+ optional ``headers``); ``kind:
"file"`` sources carry a ``base64_string`` + ``filename``.

The LiteLLM document-conversion interface uses a canonical
``DocumentConversionSource`` (``content`` + ``mime_type``), so this provider
maps each source into the upstream discriminated union: an HTTP(S) URL becomes
``kind: "http"``, a data-URI / base64 payload becomes ``kind: "file"``.

Response is the Docling ``ConvertDocumentResponse``:
    {
        "document": {"filename": "report.md"},
        "status": "success",
        "errors": [],
        "processing_time": 0.42
    }
"""

from typing import Any
from urllib.parse import urlencode

import httpx

from litellm._logging import verbose_logger
from litellm.llms.base_llm.document_conversion.transformation import (
    BaseDocumentConversionConfig,
    ConvertDocumentResponse,
    DocumentConversionRequestData,
    DocumentConversionSource,
)
from litellm.secret_managers.main import get_secret_str

DOCLING_API_KEY_ENV_VAR = "DOCLING_API_KEY"
DOCLING_API_BASE_ENV_VAR = "DOCLING_API_BASE"

DEFAULT_DOCLING_API_BASE = "http://localhost:8080"

# ``kind`` discriminator values used by the upstream Docling
# ``ConvertSourcesRequest`` source union.
SOURCE_KIND_FILE = "file"
SOURCE_KIND_HTTP = "http"

# MIME type -> filename extension used when turning a file source into an
# upstream ``filename`` (Docling's ``FileSourceRequest`` requires it).
_MIME_TO_EXT: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/tiff": ".tiff",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "text/html": ".html",
}


def _data_uri_to_base64(content: str) -> str:
    """
    Extract the base64 payload from a ``data:<mime>;base64,<data>`` URI.

    If the string is not a data URI, return it unchanged (assumed to already
    be a raw base64 string).
    """
    marker = ";base64,"
    if content.startswith("data:") and marker in content:
        return content.split(marker, 1)[1]
    return content


def _data_uri_mime(content: str) -> str | None:
    """Return the MIME type embedded in a ``data:<mime>;base64,...`` URI."""
    if not content.startswith("data:"):
        return None
    mime, _, _rest = content[5:].partition(";")
    return mime or None


def _filename_from_mime(mime_type: str | None) -> str | None:
    """
    Map a MIME type to a Docling ``filename``.

    Returns ``None`` when no MIME type is known, leaving the ``filename``
    field off the payload (Docling defaults it).
    """
    if mime_type is None:
        return None
    ext = _MIME_TO_EXT.get(mime_type.lower())
    if ext is None:
        return None
    return f"document{ext}"


# Broader-interface format names -> Docling ``to_formats`` enum values.
# Docling only accepts: md, json, yaml, html, html_split_page, text, doctags,
# vtt, doclang, dclx, chunks. Map the common aliases so callers don't have to
# know the exact enum.
_TO_FORMATS_ALIAS: dict[str, str] = {
    "markdown": "md",
    "txt": "text",
    "plain": "text",
    "plaintext": "text",
}


def _normalize_to_formats(to_formats: Any) -> Any:
    """
    Map broad-interface format names to Docling's ``to_formats`` enum values.

    Accepts a string or a list of strings. Unrecognized names pass through
    unchanged so the upstream still returns its own validation error.
    """
    if isinstance(to_formats, str):
        return _TO_FORMATS_ALIAS.get(to_formats.lower(), to_formats)
    if isinstance(to_formats, list):
        return [_TO_FORMATS_ALIAS.get(str(f).lower(), f) for f in to_formats]
    return to_formats


class DoclingDocumentConversionConfig(BaseDocumentConversionConfig):
    """
    Docling document conversion transformation configuration.
    """

    def __init__(self) -> None:
        super().__init__()

    def get_supported_document_conversion_params(self, model: str) -> list[str]:
        """
        Get supported document-conversion params for Docling.

        Docling accepts an ``options`` object with ``from_formats``,
        ``to_formats`` and ``full``. We surface ``to_formats`` (and passthrough
        of ``options``/``export_formats``) for compatibility with the broader
        interface. Arbitrary query parameters (e.g. ``debug``) are not part of
        the body; they are appended to the URL by ``get_complete_url``.
        """
        return ["to_formats", "options", "export_formats"]

    def get_api_key_env_var(self) -> str | None:
        return DOCLING_API_KEY_ENV_VAR

    def map_document_conversion_params(
        self,
        non_default_params: dict[str, Any],
        optional_params: dict[str, Any],
        model: str,
    ) -> dict[str, Any]:
        mapped_params = dict(optional_params)
        supported = self.get_supported_document_conversion_params(model=model)
        for param, value in non_default_params.items():
            if param in supported:
                mapped_params[param] = value
        # ``query_params`` is a generic pass-through channel (arbitrary URL
        # query parameters forwarded by the proxy route); always preserve it.
        if "query_params" in non_default_params:
            mapped_params["query_params"] = non_default_params["query_params"]
        return mapped_params

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
        Docling is typically a self-hosted / in-cluster service and may not
        require an API key. If a key is provided, send it as a Bearer token.
        """
        resolved_key = api_key or get_secret_str(DOCLING_API_KEY_ENV_VAR)
        headers = dict(headers)
        if resolved_key is not None:
            headers["Authorization"] = f"Bearer {resolved_key}"
        headers.setdefault("Content-Type", "application/json")
        return headers

    def get_complete_url(
        self,
        api_base: str | None,
        model: str,
        optional_params: dict,
        litellm_params: dict | None = None,
        **kwargs,
    ) -> str:
        base = (
            api_base
            or get_secret_str(DOCLING_API_BASE_ENV_VAR)
            or DEFAULT_DOCLING_API_BASE
        ).rstrip("/")
        # The controller registers the docling pod's api_base as
        # "<host>:8080/v1". If it already ends with /v1, append directly.
        url = (
            f"{base}/convert/source"
            if base.endswith("/v1")
            else f"{base}/v1/convert/source"
        )
        # Arbitrary query parameters (e.g. ``debug``) are passed through
        # generically rather than hardcoded. The proxy captures all incoming
        # query params into ``optional_params["query_params"]`` and we append
        # every one of them to the URL.
        query_params = optional_params.get("query_params")
        if isinstance(query_params, dict) and query_params:
            url = f"{url}?{urlencode(query_params)}"
        return url

    def _build_sources(
        self,
        sources: list[DocumentConversionSource],
    ) -> list[dict[str, Any]]:
        """
        Map canonical sources to Docling source items.

        Docling models each source as a discriminated union on ``kind``:

        - ``kind: "http"`` -> an HTTP(S) ``url`` (+ optional ``headers``)
        - ``kind: "file"`` -> inline ``base64_string`` + ``filename``

        A canonical ``content`` that is an HTTP(S) URL maps to the ``http``
        branch; a data-URI (``data:...;base64,...``) or raw base64 maps to the
        ``file`` branch.
        """
        out: list[dict[str, Any]] = []
        for source in sources:
            content = source.content
            if content.startswith(("http://", "https://")):
                out.append({"kind": SOURCE_KIND_HTTP, "url": content})
                continue
            if content.startswith("data:"):
                base64_string = _data_uri_to_base64(content)
                mime = source.mime_type or _data_uri_mime(content)
            else:
                base64_string = content
                mime = source.mime_type
            item: dict[str, Any] = {
                "kind": SOURCE_KIND_FILE,
                "base64_string": base64_string,
            }
            filename = _filename_from_mime(mime)
            if filename is not None:
                item["filename"] = filename
            out.append(item)
        return out

    def _build_options(self, optional_params: dict[str, Any]) -> dict[str, Any]:
        """Build the Docling ``options`` object from optional params."""
        options: dict[str, Any] = {}
        # ``export_formats`` is the broader-interface alias for ``to_formats``;
        # treat either as the Docling options ``to_formats`` value.
        to_formats = optional_params.get("to_formats") or optional_params.get("export_formats")
        if to_formats is not None:
            options["to_formats"] = _normalize_to_formats(to_formats)
        explicit_options = optional_params.get("options")
        if isinstance(explicit_options, dict):
            options.update(explicit_options)
        return options

    def transform_document_conversion_request(
        self,
        model: str,
        sources: list[DocumentConversionSource],
        optional_params: dict,
        headers: dict,
        **kwargs,
    ) -> DocumentConversionRequestData:
        """
        Build the Docling ``ConvertSourcesRequest`` body.
        """
        body: dict[str, Any] = {"sources": self._build_sources(sources)}
        options = self._build_options(optional_params)
        if options:
            body["options"] = options
        return DocumentConversionRequestData(data=body, files=None)

    def transform_document_conversion_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: Any,
        **kwargs,
    ) -> ConvertDocumentResponse:
        """
        Docling already returns the canonical ``ConvertDocumentResponse``
        shape, so we parse it directly.
        """
        # Docling switches the response to a binary payload (image/png layout
        # debug render) when a ``debug`` query param is passed. Don't try to
        # JSON-parse that; stash the raw bytes + content type so the proxy
        # route can return them verbatim (mirrors how video/container
        # endpoints surface raw provider bytes).
        content_type = (raw_response.headers.get("content-type") or "").lower()
        is_json = "json" in content_type or raw_response.text.lstrip().startswith("{")
        if not is_json:
            response = ConvertDocumentResponse(status="success")
            response._additional_properties["docling_raw"] = raw_response.content
            response._additional_properties["docling_raw_content_type"] = content_type
            return response

        response_json = raw_response.json()
        verbose_logger.debug(
            f"Docling document conversion response - model: {model}, status: {response_json.get('status')}"
        )
        response = ConvertDocumentResponse(
            document=response_json.get("document"),
            status=response_json.get("status"),
            errors=response_json.get("errors"),
            processing_time=response_json.get("processing_time"),
        )
        response._additional_properties["docling_raw"] = response_json
        return response