# Adding Docling Document Parsing as a First-Class LiteLLM Capability — Feasibility Study

> **Status**: Feasibility study (design / investigation, not yet implemented)
>
> **Method**: [logic_mapping_technique.md](../techniques/logic_mapping_technique.md)
> — trace the existing precedent end-to-end before building. This document maps
> how a brand-new first-class capability is added to LiteLLM, applies it to the
> Docling convention, and records what must change. It is a **scratchpad**: the
> analysis and the concrete plan live side by side and are meant to be edited as
> the work proceeds.
>
> **Companion guide**: [FIRST_CLASS_ENDPOINT_GUIDE.md](../techniques/FIRST_CLASS_ENDPOINT_GUIDE.md)
> — the generic, step-by-step recipe for adding a brand-new first-class path from
> the interface. This study is the concrete application of that guide to Docling.
>
> **Input documents**
> - `DOCLING_API_REFERENCE.md` (same directory) — the upstream Docling HTTP contract
> - `custom-routes/CLONE-LOGIC-MAP.md`, `custom-routes/VSA-PLAN.md` — earlier custom-route work
> - `custom-providers/LITELLM_ENDPOINT_ARCHITECTURE.md` — the 7-layer endpoint pattern
> - `custom-providers/OMNIVOICE_POST_BUILD_LOGIC_MAP.md` — what actually got wired
> - `controller/` (models.py, sources/local_deployments.py, litellm_client.py, reconciler.py)
> - `litellm/llms/base_llm/ocr/transformation.py`, `litellm/ocr/main.py`,
>   `litellm/proxy/ocr_endpoints/endpoints.py` — the OCR precedent

---

## 1. Executive summary (revised)

**LiteLLM already models document parsing as a first-class capability: OCR.** There
is a complete, working OCR subsystem — a `/v1/ocr` proxy route, a `BaseOCRConfig`
provider interface, `ProviderConfigManager.get_provider_ocr_config()`, a generic
`ocr()`/`async_ocr()` HTTP handler, a `Router.factory_function("aocr")` binding,
and four real providers (Azure Document Intelligence, Mistral, Reducto, Vertex
AI). It accepts JSON (`document_url`/`image_url`) **and** multipart file upload,
and standardizes every provider's output into a shared `OCRResponse`.

This reframes the previous pass-through-only recommendation. The gateway does
**not** need a raw pass-through hack to talk to Docling; it already models
document parsing, and Docling can be a **provider** on that path. The remaining
design decision is **which first-class concept Docling plugs into**:

- **Option 1 — Docling as an OCR provider** (smallest, reuses everything): add a
  `docling` branch to `get_provider_ocr_config()` and a
  `litellm/llms/docling/ocr/transformation.py` that maps Docling's
  `ConvertDocumentResponse` into the standard `OCRResponse`. One route (`/v1/ocr`)
  serves all parsers. **Downside**: Docling's convention (export formats, layout
  boxes, `debug` PNG, async submit/poll) is richer than Mistral-style OCR, so the
  mapping may be lossy or awkward.
- **Option 2 — new first-class "document conversion" interface** (clean, matches
  Docling and cloud providers like Unstructured.io): build a
  `BaseDocumentConversionConfig` interface + a `/v1/convert` path, following the
  OCR blueprint in `FIRST_CLASS_ENDPOINT_GUIDE.md`. Docling is the first provider.
  **This is the recommended option** if document conversion is a first-class
  product surface (as the gateway's unification goal implies).

**Recommendation**: Option 2, implemented as a minimal vertical slice in the same
style the OCR subsystem used, starting with `/v1/convert/source` (JSON, URL/base64
— the simplest, no-multipart path that exercises the whole interface). Pass-through
is no longer the primary recommendation; it remains only as an interim shim while
the first-class path is built.

> **DECISION (2026-08-17): Option 2.** Build a new first-class **document
> conversion** interface (`BaseDocumentConversionConfig`) and a `/v1/convert` path,
> with Docling as the first provider. First slice: `/v1/convert/source`. This is
> the chosen path; the Option 1/2 comparison above is retained only as rationale.
> The rest of this study assumes Option 2.

---

## 2. Why this changed: the OCR precedent, traced

Applying logic-mapping Phase 1 to the existing OCR subsystem gives the exact
recipe. Every segment is already implemented for a file-parsing capability, so
docling can mirror it file-for-file.

```
POST /v1/ocr  (JSON: model + document{type, document_url}; or multipart: file)
   |
   v
[proxy/ocr_endpoints/endpoints.py]  ocr()
   |  _parse_ocr_request()  -> JSON body OR multipart (file -> base64 data URI)
   |  ProxyBaseLLMRequestProcessing.base_process_llm_request(route_type="aocr")
   v
[proxy/common_request_processing.py]  base_process_llm_request()
   |  auth, pre-call hooks, headers, logging
   v
[proxy/route_llm_request.py]  route_request()  route_type="aocr"
   |  ROUTE_ENDPOINT_MAPPING["aocr"]="/ocr"; falls to Router (model-routed)
   v
[router.py]  _initialize_ocr_search_endpoints()
   |  self.aocr = factory_function(aocr, call_type="aocr")
   |  _ageneric_api_call_with_fallbacks(...)
   v
[ocr/main.py]  aocr()  (async)  /  ocr()  (sync)
   |  _prepare_ocr_request(): resolve provider, map params, validate
   |  base_llm_http_handler.ocr(...)
   v
[llms/custom_httpx/llm_http_handler.py]  ocr()/async_ocr()
   |  _prepare_ocr_request(): provider_config.validate_environment()
   |                            -> get_complete_url()
   |                            -> transform_ocr_request() -> (data, files)
   |  HTTP POST
   |  _transform_ocr_response(): provider_config.transform_ocr_response()
   v
[llms/<provider>/ocr/transformation.py]  <Provider>OCRConfig(BaseOCRConfig)
   |  get_complete_url -> {base}/parse  (e.g. Reducto)
   |  transform_ocr_request  /  transform_ocr_response
   v
[upstream provider]
```

**Segments that are generic and reused**: route parsing (JSON+multipart), common
request processing, routing, handler. **Segments that are new per capability**:
the interface (`BaseOCRConfig`), the SDK entry (`aocr`/`ocr`), and per-provider
configs. Docling needs exactly those same three, plus a route if we go with a new
concept (Option 2).

### Key design facts learned from OCR

1. **Standard output shape.** OCR normalizes to Mistral's format
   (`OCRResponse { pages: [OCRPage{index, markdown, images, dimensions}], model, usage_info }`).
   A new document-conversion concept should pick Docling's `ConvertDocumentResponse`
   as its canonical shape and make every provider map into it.
2. **JSON + multipart both supported.** `_parse_ocr_request` handles JSON bodies
   and multipart `files=` uploads, converting uploaded bytes to a base64 `data:`
   URI. `/v1/convert/source` is the JSON case; `/v1/convert/file` is the multipart
   case. Both can share the same interface + handler.
3. **Model-routed through the Router, but no hand-written method.** OCR uses
   `factory_function("aocr")` → `_ageneric_api_call_with_fallbacks`. So a
   "document conversion" SDK function can be bound to the Router the same way,
   without inventing new routing semantics.
4. **Provider dispatch is one function.** `ProviderConfigManager.get_provider_ocr_config()`
   maps `LlmProviders` → config. Adding Docling is one branch (or a new
   `get_provider_document_conversion_config()` if we add a new concept).
5. **Types are enumerated.** `CallTypes`, `CallTypesLiteral`,
   `API_ROUTE_TO_CALL_TYPES`, `ROUTE_ENDPOINT_MAPPING`, and the `route_type`
   literal in `route_request` all need entries.

---

## 3. The concrete plan for Docling (Option 2 — new document-conversion interface)

Follow the guide's checklist, with Docling as the first provider. Route first:
**`/v1/convert/source`** (JSON), then `/v1/convert/file` (multipart).

### New files

| # | File | What | Reuse |
|---|------|------|-------|
| 1 | `litellm/llms/base_llm/document_conversion/transformation.py` | `BaseDocumentConversionConfig`, `ConvertDocumentRequest`, `ConvertDocumentResponse`, `ConversionStatus`, `ErrorItem`, `DocResultData` (canonical = Docling shape) | pattern from `base_llm/ocr/transformation.py` |
| 2 | `litellm/llms/docling/document_conversion/transformation.py` | `DoclingDocumentConversionConfig(BaseDocumentConversionConfig)` | pattern from `reducto/ocr/transformation.py` |
| 3 | `litellm/document_conversion/main.py` + `__init__.py` | `aconvert()` / `convert()` SDK fns | pattern from `litellm/ocr/main.py` |
| 4 | `litellm/proxy/document_conversion_endpoints/endpoints.py` | `@router.post("/v1/convert/source")` (+ `/v1/convert/file`) | pattern from `ocr_endpoints/endpoints.py` |

### Modified files

| File | Change |
|------|--------|
| `litellm/utils.py` | add `ProviderConfigManager.get_provider_document_conversion_config()` |
| `litellm/types/utils.py` | `CallTypes.convert/aconvert`, `CallTypesLiteral`, `API_ROUTE_TO_CALL_TYPES["/v1/convert"]`, and `LlmProviders.DOCLING` if not present |
| `litellm/llms/custom_httpx/llm_http_handler.py` | add `document_conversion()`/`async_document_conversion()` handler |
| `litellm/router.py` | `factory_function(aconvert, call_type="aconvert")` + `_initialize_document_conversion_endpoints()` |
| `litellm/proxy/route_llm_request.py` | `"aconvert": "/convert"` in `ROUTE_ENDPOINT_MAPPING`; `"aconvert"` in `route_type` literal |
| `litellm/proxy/proxy_server.py` | `from ...document_conversion_endpoints.endpoints import router as dc_router; app.include_router(dc_router)` |

### Controller changes (discovery correctness)

- `controller/models.py detect_provider()` — add a `docling` (or `paddlex`)
  keyword so the docling pod is classified as a document-conversion provider, not
  `hosted_vllm`.
- `detect_mode_from_paths()` — if openapi paths include `/v1/convert/*`, use a
  dedicated mode (e.g. `document_conversion`) instead of `chat`.

### What is deliberately NOT touched

- `litellm/main.py` chat/audio dispatch (docling is not an audio/chat provider)
- The audio/chat provider configs
- Static pass-through config — kept only as a fallback shim

---

## 4. Endpoint inventory to expose

| Docling path | Method | Gateway path | Priority |
|--------------|--------|--------------|----------|
| `/v1/convert/source` | POST | `/v1/convert/source` | **First** (JSON, exercises interface) |
| `/v1/convert/file` | POST | `/v1/convert/file` | Second (multipart) |
| `/v1/convert/source/async` | POST | `/v1/convert/source/async` | Later |
| `/v1/convert/file/async` | POST | `/v1/convert/file/async` | Later |
| `/v1/status/poll/{task_id}` | GET | `/v1/status/poll/{task_id}` | Later (async) |
| `/v1/result/{task_id}` | GET | `/v1/result/{task_id}` | Later |
| `/v1/models` | GET | (already used by controller) | N/A |

The `debug` parameter asymmetry (query param on `/source`, form field on `/file`)
is preserved by the interface (the handler forwards the body + query). For the
first slice only `/v1/convert/source` is built; async + batch variants come after
the sync path is validated.

---

## 5. Testing plan (logic-mapping Phases 2 & 4)

- **Interface/contract test**: `BaseDocumentConversionConfig` abstract methods +
  canonical response model; must fail if mutated.
- **Docling provider test**: `DoclingDocumentConversionConfig` `get_complete_url`,
  `transform_document_conversion_request`/`response` against real Docling samples
  (see `DOCLING_API_REFERENCE.md` §5.1 shapes and §9 curl examples).
- **Handler test**: `base_llm_http_handler.document_conversion()` against a mock
  `httpx` response.
- **Endpoint test**: `/v1/convert/source` (JSON) + `/v1/convert/file` (multipart),
  auth, error mapping.
- **E2E**: run proxy on `localhost:4000`, `curl /v1/convert/source` (and later
  `/file`) against a real Docling pod, showing command + output. Verify JSON
  `document: {type, document_url}` and multipart `-F file=@`.
- **Mutation**: each test must fail when the code is mutated.

---

## 6. Risks & open questions

- **Docling vs OCR shape mismatch.** Docling's `ConvertDocumentResponse`
  (markdown, processing_time, errors) differs from Mistral-style `OCRResponse`
  (pages). Option 1 forces a lossy mapping; Option 2 keeps the Docling shape
  canonical but adds a second document interface. Verify with the customer which
  convention end clients expect.
- **Async + debug.** The async submit/poll and `debug=1` PNG response add surface
  not present in OCR. Defer to a later slice.
- **Dynamic pod address.** The docling pod is controller-discovered. In Option 2
  the pod is a registered model with `litellm_params.api_base` (like other
  deployments), so the Router resolves it — same as OCR providers resolve their
  `api_base`. No static-config problem.
- **`model` parameter.** OCR is model-parameterized. For document conversion,
  what is the `model`? Probably the docling model id (e.g. `docling/PP-DocLayoutV3`),
  matching how the controller registers it. Confirm the client contract.
- **Cost/pricing.** No token usage; decide whether conversion is `cost_per_request`
  or unclassified in spend tracking.

---

## Scratchpad (work in progress)

- OCR is the definitive precedent and is already first-class — document parsing is
  not new to LiteLLM.
- `litellm/ocr/main.py` shows the SDK + `convert_file_document_to_url_document`
  (file → base64 `data:` URI) pattern reused by a new convert SDK.
- `litellm/router.py` `_initialize_ocr_search_endpoints()` + `factory_function`
  is the template for binding a multi-provider SDK fn to the Router.
- Reducto OCR config (`llms/reducto/ocr/transformation.py`) is the cleanest
  single-provider template for `DoclingDocumentConversionConfig`.
- `litellm/proxy/ocr_endpoints/endpoints.py` shows both JSON + multipart parsing
  in one endpoint and is the template for `/v1/convert`.
- **Decision: Option 2** (new `document_conversion` interface) is chosen. Canonical
  response shape: Docling-native `ConvertDocumentResponse`. Next step is writing
  the interface.

### Next steps

1. Read `llms/reducto/ocr/transformation.py` + `ocr/main.py` fully; copy their
   shape for `document_conversion`.
2. Build the first slice: `BaseDocumentConversionConfig` interface + `aconvert`/`convert`
   SDK fns + handler + `/v1/convert/source`.
3. Add `DoclingDocumentConversionConfig` provider config + `get_provider_document_conversion_config()`.
4. Wire types (`CallTypes.convert/aconvert`, `CallTypesLiteral`, `api_route_to_call_types`,
   `ROUTE_ENDPOINT_MAPPING`, `route_type` literal) + router `factory_function` + proxy include_router.
5. Wire controller `detect_provider`/`detect_mode_from_paths` for docling.
6. Run the curl e2e against a live proxy.