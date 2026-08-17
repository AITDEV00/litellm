# Creating a New First-Class API Path in LiteLLM — From Interface to Endpoint

> **Purpose**: A unified, code-first guide for adding a **completely new,
> first-class** endpoint path to the LiteLLM gateway when no interface for that
> capability exists yet. It walks the full flow using the **logic mapping
> technique** (trace the existing precedent end-to-end, then build the new
> capability to match).
>
> **Worked example**: the existing **OCR subsystem** (`/v1/ocr`) is used as the
> reference because it is the closest precedent to document conversion / parsing:
> it is a non-chat, model-parameterized, multi-provider capability with file
> upload, and it already follows every step documented here. When this guide says
> "copy what OCR does," the OCR files are listed so you can read the real code.
>
> **Not covered here**: adding an endpoint that fits an *existing* interface
> (that is a one-file provider config + registration — see
> `custom-providers/LITELLM_ENDPOINT_ARCHITECTURE.md`). This guide is for when
> you are introducing a **brand-new interface** for a capability LiteLLM does not
> model yet.

---

## 1. When this guide applies

You are adding a new capability to the gateway, e.g. a new "document conversion"
convention (Docling / Unstructured.io), a new media type, a new retrieval
backend. The deciding question:

**Is there already a LiteLLM concept (interface + handler + SDK fn + route) for
this capability?**

| Situation | Action |
|-----------|--------|
| A base config + handler + SDK fn + proxy route already exist (e.g. OCR for document parsing) | Follow the **provider-only** path: new config class + registration. Not this guide. |
| No interface exists; you are inventing a new capability (e.g. a generic "document conversion" concept distinct from OCR) | **Follow this guide** — build the interface first, then the minimal path that consumes it. |

> **Before you start, survey the codebase.** `litellm/llms/base_llm/` contains
> the existing capability interfaces (`text_to_speech/`, `audio_transcription/`,
> `image_generation/`, `ocr/`, `files/`, `rerank/`, `search/`, `videos/`,
> `vector_store/`, `containers/`, `skills/`, `interactions/`, `agents/`, ...).
> If what you want is close to one of these (e.g. document parsing ≈ `ocr/`),
> strongly prefer **extending that existing interface** over inventing a parallel
> one. Only build a new interface when the semantics are genuinely different.

---

## 2. The architecture: a first-class path is a 7-segment chain

Every first-class capability in LiteLLM is the same chain, whether it is
chat-completions or OCR. Trace it once (logic-mapping Phase 1) and you know where
everything plugs in:

```
Client HTTP request
   |
   v
[A] Proxy FastAPI route            proxy_server.py / dedicated endpoints module
   |  parse body (JSON or multipart), auth via user_api_key_auth
   v
[B] Common request processing       ProxyBaseLLMRequestProcessing.base_process_llm_request()
   |  inject litellm params, run pre-call hooks, build headers
   v
[C] Route dispatcher                route_llm_request.py  route_request(data, route_type)
   |  model routing rules; falls through to litellm.<fn> or llm_router.<fn>
   v
[D] Router (optional)               router.py  Router.<fn> via factory_function()
   |  fallbacks / retries / deployment lookup (only when model-routed)
   v
[E] SDK function                    litellm/<capability>/main.py  def <fn>()/async def a<fn>()
   |  resolve provider, validate, build optional params, dispatch to handler
   v
[F] HTTP handler                    llms/custom_httpx/llm_http_handler.py
   |  validate_environment, get_complete_url, transform_*_request, HTTP call,
   |  transform_*_response  (all delegated to the provider config)
   v
[G] Provider config                 llms/<provider>/<capability>/transformation.py
   |  XConfig(BaseXConfig): the per-provider request/response shaping
   |
   v
[upstream provider pod / cloud]
```

**Key realisation for a new capability**: segments 1, 2, 3, 5, 6 are largely
**generic** — they only differ from chat/audio in which SDK function and which
provider config they call. The two things that are genuinely **new** are:

- **G — the provider config interface** (the abstract contract every provider
  implements), and
- **E — the SDK function** (the single entry point every route / router delegates
  to).

So "build a new first-class path" really means: **define the interface, then
write the thin generic glue that connects a route to it via one SDK function.**
The OCR subsystem is proof: its route [1] is a ~150-line endpoint, its handler
[F] is a generic `ocr()`/`async_ocr()`, and every provider only implements
`BaseOCRConfig` (segment [G]).

---

## 3. The logic map of the reference (OCR)

These are the **actual files** that implement `/v1/ocr`, read them in this order
before writing anything (this is the "borrow from the existing codebase" step):

| Segment | File | What it shows |
|---------|------|---------------|
| Interface | `litellm/llms/base_llm/ocr/transformation.py` | `BaseOCRConfig`, `OCRResponse`, `OCRPage`, `OCRRequestData` (the contract) |
| Provider config | `litellm/llms/reducto/ocr/transformation.py` | A provider implementing `BaseOCRConfig` |
| Provider dispatch | `litellm/utils.py` `ProviderConfigManager.get_provider_ocr_config()` | Provider → config mapping |
| SDK function | `litellm/ocr/main.py` | `ocr()` (sync) + `aocr()` (async), dispatch to handler |
| Handler | `litellm/llms/custom_httpx/llm_http_handler.py` | `ocr()` / `async_ocr()`, `_prepare_ocr_request()`, `_transform_ocr_response()` |
| Router | `litellm/router.py` | `_initialize_ocr_search_endpoints()` → `factory_function(aocr, call_type="aocr")` |
| Route | `litellm/proxy/ocr_endpoints/endpoints.py` | `@router.post("/v1/ocr")` endpoint |
| Types | `litellm/types/utils.py` | `CallTypes.ocr`/`aocr`, `CallTypesLiteral`, `API_ROUTE_TO_CALL_TYPES["/v1/ocr"]` |
| Dispatcher | `litellm/proxy/route_llm_request.py` | `"aocr": "/ocr"` in `ROUTE_ENDPOINT_MAPPING`, `"aocr"` in the route_type literal |
| App wiring | `litellm/proxy/proxy_server.py` | `from ...ocr_endpoints.endpoints import router as ocr_router; app.include_router(ocr_router)` |

---

## 3. Step-by-step: build a new interface and its first path

Assume the new capability is **`<capability>`** (e.g. `document_conversion`),
with provider configs `<Provider>Config` and SDK fns `convert()`/`aconvert()`.
The steps below mirror exactly what OCR does. Do them **in this order** so each
step is independently testable (vertical-slice approach).

### Step 1 — Define the provider interface (segment G, first)

Create the base contract that every provider must implement:

```
litellm/llms/base_llm/<capability>/
    __init__.py
    transformation.py       # Base<Capability>Config, request data + response models
```

Follow `litellm/llms/base_llm/ocr/transformation.py` precisely:

```python
# Base<Capability>Config(ABC / plain class)
class Base<Capability>Config:
    def get_supported_<capability>_params(self, model: str) -> list: ...
    def map_<capability>_params(self, non_default_params, optional_params, model) -> dict: ...
    def validate_environment(self, headers, model, api_key, api_base, litellm_params, **kwargs) -> dict: ...
    def get_complete_url(self, api_base, model, optional_params, litellm_params, **kwargs) -> str: ...
    def transform_<capability>_request(self, model, <input>, optional_params, headers, **kwargs) -> <Capability>RequestData: ...
    def transform_<capability>_response(self, model, raw_response, logging_obj, **kwargs) -> <Capability>Response: ...
    # optional: async_transform_*_request / async_transform_*_response for providers that need
    # async preprocessing or result polling (OCR does this for Azure Document Intelligence)
```

Define the **standard request/response models** (the unified convention that
every provider maps into). OCR standardized on Mistral's format:
`OCRResponse { pages: [OCRPage{index, markdown, images, dimensions}], model, usage_info, ... }`.

> **Design decision**: pick the **canonical, neutral** shape. For document
> conversion this is the Docling `ConvertDocumentResponse` shape (see
> `custom-providers/DOCLING_API_REFERENCE.md` §5.1). Providers map *their* native
> shape into this one, so all backends look identical through the gateway.

### Step 2 — Add the provider dispatch (segment [G] registration)

In `litellm/utils.py`, add a `ProviderConfigManager.get_provider_<capability>_config()`:

```python
@staticmethod
def get_provider_<capability>_config(model, provider: LlmProviders) -> Base<Capability>Config | None:
    if provider == litellm.LlmProviders.REDUCTO:
        from litellm.llms.reducto.<capability>.transformation import ReductoConfig
        return ReductoConfig()
    # ... one branch per provider
    return None
```

Add the provider to the `LlmProviders` enum in `litellm/types/utils.py` if new.

### Step 3 — Write the SDK function (segment [E])

Create `litellm/<capability>/main.py` with sync + async entry points. Both do the
same thing: resolve the provider via `ProviderConfigManager`, map params, then
delegate to the shared handler. Copy `litellm/ocr/main.py` (the `_prepare_<capability>_request`
→ `base_llm_http_handler.<capability>()` shape):

```python
@client
async def aconvert(model, <input>, ..., **kwargs) -> <Capability>Response:
    prepared = _prepare_convert_request(model=model, input=input, ...)
    return await base_llm_http_handler.<capability>(..., aoconvert=True)

@client
def convert(model, <input>, ..., **kwargs):
    prepared = _prepare_convert_request(model=model, input=input, ...)
    return base_llm_http_handler.<capability>(..., aoconvert=False)
```

> Note: `litellm/ocr/main.py` exposes `aocr`/`ocr` and imports them into the
> `litellm` namespace. `litellm/ocr/__init__.py` re-exports them.

### Step 4 — Add the generic HTTP handler (segment [F])

Add a `<capability>` method to `litellm/llms/custom_httpx/llm_http_handler.py`,
mirroring `ocr()`/`async_ocr()`: `_prepare_<capability>_request()` calls
`provider_config.validate_environment()` / `get_complete_url()` /
`transform_<capability>_request()`; make the HTTP call; then
`transform_<capability>_response()`. This is the **only** place that touches
httpx; it stays provider-agnostic.

### Step 5 — Route the SDK function through the router (segment [D]) — only if model-routed

If the capability is routed by `model` (like chat/OCR), wire the SDK function
into the Router via `factory_function`. OCR does:

```python
# litellm/router.py  _initialize_ocr_search_endpoints()
self.aocr = self.factory_function(aocr, call_type="aocr")
self.ocr  = self.factory_function(ocr,  call_type="ocr")
```

and adds `"aocr"`, `"ocr"` to `factory_function`'s `call_type` literal + the
sync/async wrapper dispatch. If the capability is **not** model-routed (a pure
utility endpoint), you can dispatch straight to `litellm.<fn>` and skip the
Router entirely.

### Step 6 — Route + dispatch (segments [1], [2], types)

Register the HTTP path:

**a. Types** (`litellm/types/utils.py`):
- `CallTypes`: add `convert = "convert"`, `aconvert = "aconvert"`
- `CallTypesLiteral`: add `"convert"`, `"aconvert"`
- `API_ROUTE_TO_CALL_TYPES`: add `"/v1/convert": [CallTypes.aconvert, CallTypes.convert]`

**b. Dispatcher** (`litellm/proxy/route_llm_request.py`):
- add `"aconvert": "/convert"` to `ROUTE_ENDPOINT_MAPPING`
- add `"aconvert"` to the `route_type: Literal[...]` in `route_request()`

**c. Endpoint** — create `litellm/proxy/<capability>_endpoints/endpoints.py`
following `litellm/proxy/ocr_endpoints/endpoints.py`:

```python
@router.post("/v1/convert", dependencies=[Depends(user_api_key_auth)], ...)
@router.post("/convert", dependencies=[Depends(user_api_key_auth)], ...)
async def convert(request, fastapi_response, user_api_key_dict=Depends(user_api_key_auth)):
    data = await _parse_<capability>_request(request)   # JSON and/or multipart
    processor = ProxyBaseRequestProcessing(data=data)
    return await processor.base_process_llm_request(request=request, ...,
        route_type="aconvert", ...)
```

The endpoint parses the body (JSON body and/or multipart file), then calls
`base_process_llm_request` with `route_type="aconvert"`, which handles auth, hooks,
routing, logging, headers, streaming, and errors for you.

**d. App wiring** — in `litellm/proxy/proxy_server.py`:
```python
from litellm.proxy.<capability>_endpoints.endpoints import router as <capability>_router
app.include_router(<capability>_router)
```

---

## 4. Checklist — every touch point for a new capability

| # | File | Change |
|---|------|--------|
| 1 | `litellm/lls/base_llm/<capability>/transformation.py` | `Base<Capability>Config` + models (the interface) |
| 2 | `litellm/utils.py` | `ProviderConfigManager.get_provider_<capability>_config()` |
| 3 | `litellm/types/utils.py` | `LlmProviders` entry, `CallTypes`, `CallTypesLiteral`, `API_ROUTE_TO_CALL_TYPES` |
| 4 | `litellm/constants.py` | `openai_compatible_providers` / custom-handler set if the provider needs it |
| 5 | `litellm/<capability>/main.py` + `__init__.py` | `a<fn>()` / `<fn>()` SDK fns |
| 6 | `litellm/llms/custom_httpx/llm_http_handler.py` | `<capability>()` / `async_<capability>()` handler |
| 7 | `litellm/router.py` | `factory_function` entry + `_initialize_<capability>_endpoints()` (only if model-routed) |
| 8 | `litellm/proxy/route_llm_request.py` | `ROUTE_ENDPOINT_MAPPING` + `route_type` literal |
| 9 | `litellm/proxy/<capability>_endpoints/endpoints.py` | the `@router.post("/v1/...")` endpoint |
| 10 | `litellm/proxy/proxy_server.py` | `import` + `app.include_router(...)` |
| 11 | `litellm/lls/<provider>/<capability>/transformation.py` | the actual provider config |

For a **first** capability you usually add 1 + 2 + 5 + 6 + 8 + 9 + 10 + 11 and
one provider (e.g. docling). Steps 3, 4, 7 exist so the path is a proper
first-class citizen (auth via `CallTypes`, routing via `route_request`).

---

## 5. Testing (logic-mapping Phases 2 & 4)

- **Per-provider unit test** in `tests/test_litellm/` mirroring the existing
  OCR provider tests, covering `map_<capability>_params`, `validate_environment`,
  `get_complete_url`, `transform_<capability>_request`/`response`. It must fail if
  the interface is mutated.
- **Handler test**: `base_llm_http_handler.<capability>` against a mock `httpx`
  response, asserting the request URL/body/headers and the response transform.
- **Endpoint test**: `proxy/<capability>_endpoints/endpoints.py` for JSON +
  multipart, auth, and error mapping.
- **Mutation check**: each test must fail if the feature code is mutated — do not
  write tests that pass no matter what (per repo CLAUDE.md).
- **E2E**: run the proxy on `localhost:4000` and `curl` the new endpoint against a
  real provider, showing the command and output.

---

## 6. Checklist

- [ ] Surveyed `litellm/llms/base_llm/` — confirmed no existing interface covers this
- [ ] Defined `Base<Capability>Config` + standard request/response models
- [ ] Added `ProviderConfigManager.get_provider_<capability>_config()` + provider enum
- [ ] Wrote SDK `aconvert()`/`convert()`
- [ ] Added `<capability>()`/`async_<capability>()` handler
- [ ] Registered types + `route_request` + `CallTypes`
- [ ] Created proxy endpoint and wired via `app.include_router`
- [ ] Router `factory_function` wired (only if model-routed)
- [ ] At least one real provider config (e.g. docling)
- [ ] Unit + endpoint + e2e tests pass, including mutation checks