# OmniVoice Custom Routes: Logic Map

> **Scope**: Full 7-layer trace of the existing `/v1/audio/speech/clone` route, used as the reference implementation pattern for the missing OmniVoice routes (`/v1/audio/script`, `/v1/voices`, `/v1/voices/profiles`).
>
> **Methodology**: [logic_mapping_technique.md](../logic_mapping_technique.md) Phase 1 (Trace)

## 7-Layer Architecture Overview

```
Client Request
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│ Layer 1: Proxy Route (proxy_server.py)                  │
│   FastAPI @router.post("/v1/audio/speech/clone")        │
│   Parses multipart form, extracts ref_audio file        │
│   Calls route_request(route_type="aspeech")             │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ Layer 2: Route Dispatcher (route_llm_request.py)        │
│   route_request(data, route_type="aspeech")             │
│   ROUTE_ENDPOINT_MAPPING["aspeech"] = "/audio/speech"   │
│   Dispatches to llm_router.aspeech(**data)              │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ Layer 3: Router Method (router.py)                      │
│   Router.aspeech(model, input, voice, **kwargs)         │
│   → async_function_with_fallbacks → _aspeech            │
│   _aspeech: gets deployment, calls litellm.aspeech()    │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ Layer 4: SDK Function (main.py)                         │
│   litellm.aspeech(*args, **kwargs)                      │
│   → loop.run_in_executor → speech()                     │
│   speech(): detects custom_llm_provider == "omnivoice"  │
│   Detects kwargs["ref_audio"] → OmniVoiceVoiceCloneConfig│
│   Calls base_llm_http_handler.text_to_speech_handler()  │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ Layer 5: HTTP Handler (llm_http_handler.py)             │
│   async_text_to_speech_handler()                        │
│   Calls config.transform_text_to_speech_request()       │
│   Checks request_data for body type:                    │
│     "dict_body" → JSON POST                             │
│     "form_data" → multipart POST (clone uses this)      │
│   Returns config.transform_text_to_speech_response()    │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ Layer 6: Provider Config (voice/transformation.py)      │
│   OmniVoiceVoiceCloneConfig:                             │
│     get_complete_url() → "{base}/v1/audio/speech/clone" │
│     transform_text_to_speech_request():                  │
│       pops ref_audio, builds form_fields dict            │
│       returns TextToSpeechRequestData(form_data=...,     │
│                                       files=...)         │
│     transform_text_to_speech_response():                 │
│       wraps httpx.Response → HttpxBinaryResponseContent  │
└────────────────────────┬────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────┐
│ Layer 7: Response (proxy_server.py)                     │
│   StreamingResponse(_audio_speech_chunk_generator,      │
│                     media_type="audio/mpeg")             │
└─────────────────────────────────────────────────────────┘
```

## Layer 1: Proxy Route — `proxy_server.py:9133`

**File**: `litellm/proxy/proxy_server.py`

```python
@router.post(
    "/v1/audio/speech/clone",
    dependencies=[Depends(user_api_key_auth)],
    tags=["audio"],
)
@router.post(
    "/audio/speech/clone",
    dependencies=[Depends(user_api_key_auth)],
    tags=["audio"],
)
async def audio_speech_clone(request, fastapi_response, user_api_key_dict):
```

### Flow (line-by-line)

| Step | Line | What happens |
|---|---|---|
| 1 | 9155 | `form_data = await get_form_data(request)` — parse multipart |
| 2 | 9156 | `data = {k: v for k, v in form_data.items() if k != "ref_audio"}` — all form fields except the file |
| 3 | 9158 | `data = await add_litellm_data_to_request(...)` — injects litellm internal params (headers, org, timeout, secret_fields, etc.) |
| 4 | 9168 | `data["user"] = user_api_key_dict.user_id` if missing |
| 5 | 9171 | `data["model"] = user_model` if set globally |
| 6 | 9173-9183 | Read `ref_audio` UploadFile → `data["ref_audio"] = (filename, content_bytes, content_type)` tuple |
| 7 | 9185-9192 | Pop `text` from data, set `data["input"] = text_input`, `data.setdefault("voice", "clone")` |
| 8 | 9194 | `data = await proxy_logging_obj.pre_call_hook(call_type="aspeech")` |
| 9 | 9199 | `llm_call = await route_request(data, route_type="aspeech", ...)` |
| 10 | 9203 | `response = await llm_call` |
| 11 | 9205 | `asyncio.create_task(update_request_status("success"))` |
| 12 | 9209-9212 | Extract `_hidden_params` from response (model_id, cache_key, api_base, response_cost, litellm_call_id) |
| 13 | 9214-9226 | Build `custom_headers` via `ProxyBaseLLMRequestProcessing.get_custom_headers()` |
| 14 | 9228-9234 | `post_call_response_headers_hook` for callback headers |
| 15 | 9236 | `return StreamingResponse(_audio_speech_chunk_generator(response), media_type="audio/mpeg", headers=custom_headers)` |

### Key observations

- The route is a thin HTTP handler. It does NOT call the provider directly. It delegates to `route_request` which goes through the full router → SDK → handler → config chain.
- `ref_audio` is read from the UploadFile and stored as a `FileTypes` tuple `(filename, bytes, content_type)` in `data["ref_audio"]`. This tuple flows through all layers down to the config's `transform_text_to_speech_request`.
- `voice` is set to `"clone"` as a sentinel value. The pod ignores it; it's there because the `speech()` SDK function signature requires `voice: str`.
- `text` is renamed to `input` because the SDK `speech()` function uses `input` as the parameter name.
- The response is always `audio/mpeg` via `StreamingResponse`, regardless of the actual response_format requested.

## Layer 2: Route Dispatcher — `route_llm_request.py:68`

**File**: `litellm/proxy/route_llm_request.py`

```python
ROUTE_ENDPOINT_MAPPING = {
    ...
    "aspeech": "/audio/speech",
    "acreate_voice": "/audio/voices",
    ...
}
```

### Flow

| Step | Line | What happens |
|---|---|---|
| 1 | 596 | `getattr(llm_router, "aspeech")(**data)` — dispatches to `Router.aspeech()` |
| 2 | 601 | `route_name = ROUTE_ENDPOINT_MAPPING.get("aspeech", "aspeech")` → `"/audio/speech"` |

The dispatcher uses `getattr(llm_router, route_type)(**data)` for model-routed calls. The `aspeech` route_type maps to `Router.aspeech()`.

## Layer 3: Router Method — `router.py:3751`

**File**: `litellm/router.py`

```python
async def aspeech(self, model: str, input: str, voice: str, **kwargs):
    kwargs["model"] = model
    kwargs["input"] = input
    kwargs["voice"] = voice
    kwargs["original_function"] = self._aspeech
    self._update_kwargs_before_fallbacks(model=model, kwargs=kwargs)
    response = await self.async_function_with_fallbacks(**kwargs)
    return response

async def _aspeech(self, model: str, input: str, voice: str, **kwargs):
    deployment = await self.async_get_available_deployment(model=model, ...)
    self._update_kwargs_with_deployment(deployment=deployment, kwargs=kwargs)
    data = deployment["litellm_params"].copy()
    model_client = self._get_async_openai_model_client(deployment=deployment, kwargs=kwargs)
    response = litellm.aspeech(**{**data, "input": input, "voice": voice, "client": model_client, **kwargs})
    ...
    response = await response
    return response
```

### Flow

| Step | Line | What happens |
|---|---|---|
| 1 | 3751 | `Router.aspeech(model, input, voice, **kwargs)` — entry point |
| 2 | 3759 | Sets `kwargs["original_function"] = self._aspeech` for fallback handling |
| 3 | 3761 | `async_function_with_fallbacks(**kwargs)` — handles fallback chains |
| 4 | 3767 | `_aspeech(model, input, voice, **kwargs)` — actual deployment call |
| 5 | 3813 | `async_get_available_deployment(model=model)` — picks a healthy pod |
| 6 | 3818 | `_update_kwargs_with_deployment(deployment, kwargs)` — injects api_base, api_key from deployment |
| 7 | 3820 | `data = deployment["litellm_params"].copy()` — gets provider params |
| 8 | 3822 | `_get_async_openai_model_client(deployment, kwargs)` — gets HTTP client |
| 9 | 3826 | `litellm.aspeech(**{**data, "input": input, "voice": voice, "client": model_client, **kwargs})` — calls SDK |
| 10 | 3856 | `response = await response` — awaits the coroutine |

## Layer 4: SDK Function — `main.py:7693`

**File**: `litellm/main.py`

```python
_CUSTOM_AUDIO_HANDLER_PROVIDERS: frozenset[str] = frozenset({"inception", "omnivoice"})

async def aspeech(*args, **kwargs) -> HttpxBinaryResponseContent:
    kwargs["aspeech"] = True
    ...
    func = partial(speech, *args, **kwargs)
    init_response = await loop.run_in_executor(None, func_with_context)
    if asyncio.iscoroutine(init_response):
        response = await init_response
    else:
        response = await loop.run_in_executor(None, func_with_context)
    return response

@client
def speech(model, input, voice=None, ...):
    ...
    text_to_speech_provider_config = ProviderConfigManager.get_provider_text_to_speech_config(
        model=model, provider=litellm.LlmProviders(custom_llm_provider)
    )
    ...
    elif custom_llm_provider == "omnivoice":
        if kwargs.get("ref_audio") is not None:
            text_to_speech_provider_config = OmniVoiceVoiceCloneConfig()
        else:
            text_to_speech_provider_config = OmniVoiceTextToSpeechConfig()
        ...
        response = base_llm_http_handler.text_to_speech_handler(
            model=model, input=input, voice=voice,
            text_to_speech_provider_config=text_to_speech_provider_config,
            ...
        )
```

### Flow

| Step | Line | What happens |
|---|---|---|
| 1 | 7693 | `aspeech(*args, **kwargs)` — async wrapper |
| 2 | 7701 | Sets `kwargs["aspeech"] = True` |
| 3 | 7709 | `get_llm_provider(model=model)` — resolves provider from model string |
| 4 | 7735 | `speech(model, input, voice, ...)` — sync function decorated with `@client` |
| 5 | 7793 | `ProviderConfigManager.get_provider_text_to_speech_config(provider=OMNIVOICE)` |
| 6 | 8217 | `elif custom_llm_provider == "omnivoice"` branch |
| 7 | 8218 | `if kwargs.get("ref_audio") is not None:` → **clone detection** |
| 8 | 8226 | `OmniVoiceVoiceCloneConfig()` instantiated |
| 9 | 8240 | `base_llm_http_handler.text_to_speech_handler(...)` called |

### Key insight: ref_audio detection

The clone vs standard TTS branching happens here in `speech()`. If `kwargs["ref_audio"]` exists, it's a clone. Otherwise, standard TTS. This is the single point that determines which config class processes the request.

## Layer 5: HTTP Handler — `llm_http_handler.py:11224`

**File**: `litellm/llms/custom_httpx/llm_http_handler.py`

```python
async def async_text_to_speech_handler(self, model, input, voice,
    text_to_speech_provider_config, ...):
    ...
    headers = config.validate_environment(api_key=..., headers=..., model=..., api_base=...)
    api_base = config.get_complete_url(model=..., api_base=..., litellm_params=...)
    request_data = config.transform_text_to_speech_request(
        model=..., input=..., voice=..., optional_params=..., litellm_params=..., headers=...)
    ...
    if "dict_body" in request_data:
        response = await async_httpx_client.post(url=api_base, json=request_data["dict_body"], ...)
    elif "ssml_body" in request_data:
        response = await async_httpx_client.post(url=api_base, data=request_data["ssml_body"], ...)
    elif "form_data" in request_data:
        response = await async_httpx_client.post(url=api_base, data=request_data["form_data"],
                                                 files=request_data.get("files"), ...)
    ...
    return config.transform_text_to_speech_response(model=..., raw_response=response, ...)
```

### Flow

| Step | Line | What happens |
|---|---|---|
| 1 | 11224 | `async_text_to_speech_handler(...)` entry |
| 2 | 11244 | `validate_environment(api_key, headers, model, api_base)` — OmniVoice returns headers as-is (no auth needed) |
| 3 | 11257 | `get_complete_url(model, api_base, litellm_params)` → `"{base}/v1/audio/speech/clone"` |
| 4 | 11261 | `transform_text_to_speech_request(...)` → returns `TextToSpeechRequestData(form_data=..., files=...)` |
| 5 | 11292 | `"form_data" in request_data` → true for clone |
| 6 | 11306 | `async_httpx_client.post(url=api_base, data=form_data, files=files)` — multipart POST |
| 7 | 11319 | `transform_text_to_speech_response(raw_response)` → `HttpxBinaryResponseContent` |

### Body type dispatch

The handler supports three body types via `TextToSpeechRequestData`:
- `dict_body` — JSON POST (used by standard TTS)
- `ssml_body` — raw XML body
- `form_data` + `files` — multipart POST (used by voice clone)

The config class decides which body type by setting the appropriate field on `TextToSpeechRequestData`.

## Layer 6: Provider Config — `voice/transformation.py`

**File**: `litellm/llms/omnivoice/voice/transformation.py`

```python
_CLONE_FORM_KEYS: tuple[str, ...] = (
    "response_format", "speed", "stream", "num_step", "guidance_scale",
    "denoise", "t_shift", "position_temperature", "class_temperature",
    "duration", "language", "layer_penalty_factor", "preprocess_prompt",
    "postprocess_output", "audio_chunk_duration", "audio_chunk_threshold",
    "request_timeout_s",
)

class OmniVoiceVoiceCloneConfig(OmniVoiceModelInfo, BaseTextToSpeechConfig):
    def get_complete_url(self, model, api_base, litellm_params) -> str:
        base = self._resolve_base(api_base)
        if base.lower().endswith("/v1"):
            base = base[:-3]
        return base + "/v1/audio/speech/clone"

    def transform_text_to_speech_request(self, model, input, voice,
        optional_params, litellm_params, headers) -> TextToSpeechRequestData:
        ref_audio = optional_params.pop("ref_audio", None)
        ref_text = optional_params.pop("ref_text", None)
        form_fields = {"text": input}
        if ref_text is not None:
            form_fields["ref_text"] = ref_text
        for key in _CLONE_FORM_KEYS:
            value = optional_params.pop(key, None)
            if value is not None:
                form_fields[key] = value
        _collect_passthrough(optional_params, form_fields)
        if isinstance(ref_audio, tuple):
            files = {"ref_audio": ref_audio}
        elif isinstance(ref_audio, (bytes, bytearray)):
            files = {"ref_audio": ("ref_audio.wav", bytes(ref_audio), "audio/wav")}
        else:
            processed = process_audio_file(ref_audio)
            files = {"ref_audio": (processed.filename, processed.file_content, processed.content_type)}
        return TextToSpeechRequestData(form_data=form_fields, files=files)

    def transform_text_to_speech_response(self, model, raw_response, logging_obj):
        return HttpxBinaryResponseContent(raw_response)
```

### Flow

| Step | Method | What happens |
|---|---|---|
| 1 | `validate_environment` | Returns headers as-is (no API key needed for OmniVoice) |
| 2 | `get_complete_url` | Strips trailing `/v1`, appends `/v1/audio/speech/clone` |
| 3 | `transform_text_to_speech_request` | Pops `ref_audio` from optional_params, builds form_fields dict with `text` + clone params, returns `TextToSpeechRequestData(form_data=..., files=...)` |
| 4 | (handler sends multipart POST) | httpx sends `data=form_fields, files={"ref_audio": tuple}` |
| 5 | `transform_text_to_speech_response` | Wraps raw httpx.Response in `HttpxBinaryResponseContent` |

### `_collect_passthrough` function

**File**: `litellm/llms/omnivoice/common_utils.py:79`

```python
def _collect_passthrough(source: dict[str, Any], dest: dict[str, Any]) -> None:
    for key, value in source.items():
        if value is None or key in OMNIVOICE_INTERNAL_PARAMS or key in {"extra_body", "extra_headers"}:
            continue
        if isinstance(value, (dict, list)):
            continue
        dest[key] = value
```

Copies non-internal, non-None, non-dict/list values from source to dest. This is how extra params (not in `_CLONE_FORM_KEYS`) get forwarded to the pod as form fields. Tuples are allowed (for `ref_audio`).

## Layer 7: Response — `proxy_server.py:9236`

```python
return StreamingResponse(
    _audio_speech_chunk_generator(response),
    media_type="audio/mpeg",
    headers=custom_headers,
)
```

The response from the handler (`HttpxBinaryResponseContent`) is streamed back to the client as `audio/mpeg`.

## Full Call Chain Summary

```
proxy_server.py:9142  audio_speech_clone()
    │
    ├─ get_form_data(request)                          # parse multipart
    ├─ add_litellm_data_to_request()                   # inject internal params
    ├─ read ref_audio UploadFile → tuple               # prepare file
    ├─ pre_call_hook(call_type="aspeech")              # logging/guardrails
    │
    ├─ route_request(route_type="aspeech")             # Layer 2
    │   │
    │   └─ llm_router.aspeech(**data)                  # Layer 3
    │       │
    │       ├─ async_get_available_deployment()         # pick pod
    │       ├─ _update_kwargs_with_deployment()         # inject api_base
    │       │
    │       └─ litellm.aspeech(**data)                  # Layer 4
    │           │
    │           └─ speech(model, input, voice, ...)
    │               │
    │               ├─ get_llm_provider(model)           # resolve "omnivoice"
    │               ├─ kwargs["ref_audio"] detected      # → clone config
    │               ├─ OmniVoiceVoiceCloneConfig()        # Layer 6 config
    │               │
    │               └─ text_to_speech_handler()          # Layer 5
    │                   │
    │                   ├─ validate_environment()         # no-op for omnivoice
    │                   ├─ get_complete_url()             # /v1/audio/speech/clone
    │                   ├─ transform_text_to_speech_request()
    │                   │   │
    │                   │   ├─ pop ref_audio             # extract file
    │                   │   ├─ build form_fields         # text + clone params
    │                   │   ├─ _collect_passthrough()    # extra params
    │                   │   └─ return TextToSpeechRequestData(form_data=, files=)
    │                   │
    │                   ├─ async_httpx_client.post(url, data=form_data, files=files)
    │                   │                                            # multipart POST to pod
    │                   │
    │                   └─ transform_text_to_speech_response()
    │                       └─ HttpxBinaryResponseContent(raw_response)
    │
    └─ StreamingResponse(audio/mpeg)                    # Layer 7
```

## Files Touched by the Clone Route

| Layer | File | Key symbol |
|---|---|---|
| 1 | `litellm/proxy/proxy_server.py:9133` | `audio_speech_clone()` |
| 2 | `litellm/proxy/route_llm_request.py:73` | `ROUTE_ENDPOINT_MAPPING["aspeech"]` |
| 3 | `litellm/router.py:3751` | `Router.aspeech()` / `Router._aspeech()` |
| 4 | `litellm/main.py:7693` | `aspeech()` / `speech()` |
| 4 | `litellm/main.py:7731` | `_CUSTOM_AUDIO_HANDLER_PROVIDERS` |
| 4 | `litellm/main.py:8217` | `elif custom_llm_provider == "omnivoice"` branch |
| 5 | `litellm/llms/custom_httpx/llm_http_handler.py:11224` | `async_text_to_speech_handler()` |
| 6 | `litellm/llms/omnivoice/voice/transformation.py` | `OmniVoiceVoiceCloneConfig` |
| 6 | `litellm/llms/omnivoice/common_utils.py:79` | `_collect_passthrough()` |
| 6 | `litellm/llms/omnivoice/common_utils.py:6` | `OMNIVOICE_INTERNAL_PARAMS` |
| 7 | `litellm/proxy/proxy_server.py:9236` | `StreamingResponse` |
