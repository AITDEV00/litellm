# LiteLLM Endpoint Architecture: How to Add a New First-Class Endpoint

This document is a complete trace of how LiteLLM implements standard OpenAI-compatible endpoints (like `/v1/audio/speech`, `/v1/audio/transcriptions`, `/v1/chat/completions`). It documents every layer in the chain, the exact files and functions involved, and the pattern for adding a new endpoint.

## Architecture Overview

Every endpoint in LiteLLM passes through 7 layers. The flow is:

```
Client HTTP Request
    |
    v
[1] Proxy FastAPI Route        -- proxy_server.py: @router.post("/v1/audio/speech")
    |  parses body, calls add_litellm_data_to_request(), calls route_request()
    v
[2] Route Dispatcher           -- route_llm_request.py: route_request(data, route_type="aspeech")
    |  getattr(llm_router, "aspeech")(**data)
    v
[3] Router Method              -- router.py: async def aspeech() -> async def _aspeech()
    |  resolves deployment, injects api_key/api_base, calls litellm.aspeech()
    v
[4] SDK Function               -- main.py: def speech() / async def aspeech()
    |  get_llm_provider() resolves provider from model name
    |  ProviderConfigManager.get_provider_text_to_speech_config() returns provider config
    |  provider_config.map_openai_params() maps OpenAI params to provider params
    |  dispatches to provider-specific branch (e.g. elif custom_llm_provider == "elevenlabs")
    v
[5] HTTP Handler               -- llm_http_handler.py: text_to_speech_handler()
    |  calls provider_config.validate_environment()
    |  calls provider_config.get_complete_url()
    |  calls provider_config.transform_text_to_speech_request()
    |  makes HTTP POST to provider
    |  calls provider_config.transform_text_to_speech_response()
    v
[6] Provider Config            -- llms/<provider>/text_to_speech/transformation.py
    |  HamsaTextToSpeechConfig(BaseTextToSpeechConfig)
    |  implements all abstract methods
    v
[7] Response                   -- proxy_server.py: StreamingResponse(_audio_speech_chunk_generator(response))
    |  streams binary audio back to client
```

## Layer-by-Layer Breakdown

### Layer 1: Proxy FastAPI Route

**File**: `litellm/proxy/proxy_server.py`

Every endpoint starts as a FastAPI route with the `@router.post` decorator. Two paths are always registered (with and without `/v1` prefix):

```python
@router.post(
    "/v1/audio/speech",
    dependencies=[Depends(user_api_key_auth)],
    tags=["audio"],
)
@router.post(
    "/audio/speech",
    dependencies=[Depends(user_api_key_auth)],
    tags=["audio"],
)
async def audio_speech(
    request: Request,
    fastapi_response: Response,
    user_api_key_dict: UserAPIKeyAuth = Depends(user_api_key_auth),
):
    data: Dict = {}
    try:
        # Parse body (JSON for speech, multipart form for transcription)
        body = await request.body()
        data = orjson.loads(body)

        # Inject litellm internal params (api_key, api_base, metadata, etc.)
        data = await add_litellm_data_to_request(
            data=data,
            request=request,
            general_settings=general_settings,
            user_api_key_dict=user_api_key_dict,
            version=version,
            proxy_config=proxy_config,
        )

        # Set default user from API key
        if data.get("user", None) is None and user_api_key_dict.user_id is not None:
            data["user"] = user_api_key_dict.user_id

        # Override model if set via CLI
        if user_model:
            data["model"] = user_model

        # Pre-call hooks (guardrails, logging, etc.)
        data = await proxy_logging_obj.pre_call_hook(
            user_api_key_dict=user_api_key_dict, data=data, call_type="aspeech"
        )

        # Route to the correct endpoint
        llm_call = await route_request(
            data=data,
            route_type="aspeech",
            llm_router=llm_router,
            user_model=user_model,
        )
        response = await llm_call

        # Build custom headers (cost, model_id, etc.)
        hidden_params = getattr(response, "_hidden_params", {}) or {}
        custom_headers = ProxyBaseLLMRequestProcessing.get_custom_headers(...)

        # Return streaming response
        return StreamingResponse(
            _audio_speech_chunk_generator(response),
            media_type="audio/mpeg",
            headers=custom_headers,
        )

    except Exception as e:
        await proxy_logging_obj.post_call_failure_hook(...)
```

**Key differences between JSON-body and multipart-form endpoints**:

For JSON endpoints (like `/v1/audio/speech`):
```python
body = await request.body()
data = orjson.loads(body)
```

For file-upload endpoints (like `/v1/audio/transcriptions`):
```python
form_data = await get_form_data(request)
data = {key: value for key, value in form_data.items() if key != "file"}
# ... later:
file_content = await file.read()
file_object = io.BytesIO(file_content)
file_object.name = file.filename
data["file"] = file_object
```

### Layer 2: Types and Enums

**File**: `litellm/types/utils.py`

Three things must be registered here for each endpoint:

**2a. CallTypes enum** (defines the call type names):

```python
class CallTypes(str, Enum):
    aspeech = "aspeech"       # async version
    speech = "speech"         # sync version
    atranscription = "atranscription"
    transcription = "transcription"
    # ... etc
```

**2b. CallTypesLiteral** (TypeScript-style literal type for static typing):

```python
CallTypesLiteral = Literal[
    "aspeech",
    "speech",
    "atranscription",
    "transcription",
    # ... etc
]
```

**2c. API_ROUTE_TO_CALL_TYPES** (maps URL paths to call types, used for auth/routing):

```python
API_ROUTE_TO_CALL_TYPES = {
    "/audio/speech": [CallTypes.aspeech, CallTypes.speech],
    "/v1/audio/speech": [CallTypes.aspeech, CallTypes.speech],
    "/audio/transcriptions": [CallTypes.atranscription, CallTypes.transcription],
    "/v1/audio/transcriptions": [CallTypes.atranscription, CallTypes.transcription],
    # ... etc
}
```

### Layer 3: Route Dispatcher

**File**: `litellm/proxy/route_llm_request.py`

The `route_request()` function takes the data dict and a `route_type` string, then dispatches to the router or SDK:

```python
async def route_request(
    data: dict,
    llm_router: Optional[LitellmRouter],
    user_model: Optional[str],
    route_type: Literal[
        "acompletion",
        "atext_completion",
        "aembedding",
        "aimage_generation",
        "aspeech",            # <-- each endpoint is listed here
        "atranscription",
        "amoderation",
        "arerank",
        # ... many more
    ],
    user_api_key_dict: Optional[UserAPIKeyAuth] = None,
):
    # ...
    if llm_router is not None:
        return getattr(llm_router, f"{route_type}")(**data)
    else:
        return getattr(litellm, f"{route_type}")(**data)
```

There is also a `ROUTE_ENDPOINT_MAPPING` dict that maps call types to their endpoint paths (used for logging/display):

```python
ROUTE_ENDPOINT_MAPPING = {
    "aspeech": "/audio/speech",
    "atranscription": "/audio/transcriptions",
    "acompletion": "/chat/completions",
    # ... etc
}
```

### Layer 4: Router Methods

**File**: `litellm/router.py`

The Router class has a pair of methods for each endpoint: a public method (with fallbacks/retries) and a private `_` method (the actual call):

```python
class Router:
    async def aspeech(self, model: str, input: str, voice: str, **kwargs):
        """Public method: handles fallbacks, retries, alerts."""
        kwargs["model"] = model
        kwargs["input"] = input
        kwargs["voice"] = voice
        kwargs["original_function"] = self._aspeech
        self._update_kwargs_before_fallbacks(model=model, kwargs=kwargs)
        response = await self.async_function_with_fallbacks(**kwargs)
        return response

    async def _aspeech(self, model: str, input: str, voice: str, **kwargs):
        """Private method: resolves deployment, calls litellm.aspeech()."""
        deployment = await self.async_get_available_deployment(
            model=model,
            messages=[{"role": "user", "content": "prompt"}],
            specific_deployment=kwargs.pop("specific_deployment", None),
            request_kwargs=kwargs,
        )
        self._update_kwargs_with_deployment(deployment=deployment, kwargs=kwargs)
        data = deployment["litellm_params"].copy()

        response = litellm.aspeech(
            **{
                **data,           # api_key, api_base, model from deployment config
                "input": input,
                "voice": voice,
                "client": model_client,
                **kwargs,
            }
        )
        response = await response
        return response
```

### Layer 5: SDK Function (main.py)

**File**: `litellm/main.py`

This is where the provider is resolved and the provider-specific config is fetched. Two functions: `speech()` (sync) and `aspeech()` (async).

```python
async def aspeech(*args, **kwargs) -> HttpxBinaryResponseContent:
    """Async wrapper: runs sync speech() in executor."""
    model = args[0] if len(args) > 0 else kwargs["model"]
    kwargs["aspeech"] = True
    func = partial(speech, *args, **kwargs)
    _, custom_llm_provider, _, _ = get_llm_provider(model=model, api_base=kwargs.get("api_base", None))
    init_response = await loop.run_in_executor(None, func_with_context)
    if asyncio.iscoroutine(init_response):
        response = await init_response
    else:
        response = await loop.run_in_executor(None, func_with_context)
    return response


@client
def speech(
    model: str,
    input: str,
    voice: Optional[Union[str, dict]] = None,
    api_key: Optional[str] = None,
    api_base: Optional[str] = None,
    # ... standard OpenAI params (response_format, speed, instructions)
    custom_llm_provider: Optional[str] = None,
    aspeech: Optional[bool] = None,
    **kwargs,
) -> Union[HttpxBinaryResponseContent, Coroutine[Any, Any, HttpxBinaryResponseContent]]:
    # 1. Resolve provider from model name (e.g. "hamsa/hamsa-tts" -> provider="hamsa", model="hamsa-tts")
    model, custom_llm_provider, dynamic_api_key, api_base = get_llm_provider(
        model=model, custom_llm_provider=custom_llm_provider, api_base=api_base
    )

    # 2. Build optional_params from OpenAI-standard function args
    optional_params = {}
    if response_format is not None:
        optional_params["response_format"] = response_format
    if speed is not None:
        optional_params["speed"] = speed
    if instructions is not None:
        optional_params["instructions"] = instructions

    # 3. Build litellm_params dict (metadata, call_id, etc.)
    litellm_params_dict = get_litellm_params(**kwargs)

    # 4. Get provider config from ProviderConfigManager
    text_to_speech_provider_config = ProviderConfigManager.get_provider_text_to_speech_config(
        model=model,
        provider=litellm.LlmProviders(custom_llm_provider),
    )

    # 5. Map OpenAI params to provider-specific params
    if text_to_speech_provider_config is not None:
        voice, optional_params = text_to_speech_provider_config.map_openai_params(
            model=model,
            optional_params=optional_params,
            voice=voice,
            drop_params=False,
            kwargs=kwargs,
        )

    # 6. Update logging environment
    logging_obj.update_environment_variables(
        model=model, user=user, optional_params=optional_params,
        litellm_params={...litellm_params_dict...},
        custom_llm_provider=custom_llm_provider,
    )

    # 7. Dispatch to provider-specific branch
    response = None
    if custom_llm_provider == "openai" or custom_llm_provider in litellm.openai_compatible_providers:
        # Uses OpenAI SDK directly
        response = openai_chat_completions.audio_speech(...)
    elif custom_llm_provider == "elevenlabs":
        # Uses base_llm_http_handler.text_to_speech_handler()
        response = base_llm_http_handler.text_to_speech_handler(
            model=model,
            input=input,
            voice=voice_id,
            text_to_speech_provider_config=elevenlabs_config,
            text_to_speech_optional_params=optional_params,
            custom_llm_provider=custom_llm_provider,
            litellm_params=litellm_params_dict,
            logging_obj=logging_obj,
            timeout=timeout,
            extra_headers=extra_headers,
            client=client,
            _is_async=aspeech or False,
        )
    # ... other providers

    return response
```

**Key pattern**: Providers that use the OpenAI SDK (openai, azure) call the SDK directly. Custom providers (ElevenLabs, MiniMax, etc.) use `base_llm_http_handler.text_to_speech_handler()` which is a generic HTTP handler that delegates to the provider config.

### Layer 6: HTTP Handler

**File**: `litellm/llms/custom_httpx/llm_http_handler.py`

The `text_to_speech_handler()` is a generic handler that works for any provider that follows the `BaseTextToSpeechConfig` interface. It has both sync and async versions:

```python
class LLMHTTPHandler:
    def text_to_speech_handler(
        self,
        model: str,
        input: str,
        voice: Optional[str],
        text_to_speech_provider_config: BaseTextToSpeechConfig,
        text_to_speech_optional_params: Dict,
        custom_llm_provider: str,
        litellm_params: Dict,
        logging_obj: LiteLLMLoggingObj,
        timeout: Union[float, httpx.Timeout],
        extra_headers: Optional[Dict[str, Any]] = None,
        client: Optional[Union[HTTPHandler, AsyncHTTPHandler]] = None,
        _is_async: bool = False,
    ) -> Union[HttpxBinaryResponseContent, Coroutine[...]]:
        if _is_async:
            return self.async_text_to_speech_handler(...)

        # 1. Validate environment (set auth headers)
        headers = text_to_speech_provider_config.validate_environment(
            api_key=litellm_params.get("api_key"),
            headers=extra_headers or {},
            model=model,
            api_base=litellm_params.get("api_base"),
        )

        # 2. Get complete URL
        api_base = text_to_speech_provider_config.get_complete_url(
            model=model,
            api_base=litellm_params.get("api_base"),
            litellm_params=litellm_params,
        )

        # 3. Transform request to provider format
        request_data = text_to_speech_provider_config.transform_text_to_speech_request(
            model=model,
            input=input,
            voice=voice,
            optional_params=text_to_speech_optional_params,
            litellm_params=litellm_params,
            headers=headers,
        )

        # 4. Merge provider-specific headers
        if "headers" in request_data:
            headers.update(request_data["headers"])

        # 5. Make HTTP request
        if "dict_body" in request_data:
            response = sync_httpx_client.post(
                url=api_base, headers=headers, json=request_data["dict_body"], timeout=timeout,
            )
        elif "ssml_body" in request_data:
            response = sync_httpx_client.post(
                url=api_base, headers=headers, data=request_data["ssml_body"], timeout=timeout,
            )

        # 6. Transform response
        return text_to_speech_provider_config.transform_text_to_speech_response(
            model=model, raw_response=response, logging_obj=logging_obj,
        )
```

### Layer 7: Provider Config

**File**: `litellm/llms/<provider>/text_to_speech/transformation.py`

Each provider implements a config class that inherits from the base class. Example (ElevenLabs):

```python
class ElevenLabsTextToSpeechConfig(BaseTextToSpeechConfig):
    def get_supported_openai_params(self, model: str) -> list:
        return ["voice", "response_format", "speed"]

    def map_openai_params(self, model, optional_params, voice, drop_params, kwargs):
        # Map OpenAI params to ElevenLabs params
        mapped_voice = self._resolve_voice_id(voice, params)
        # ... mapping logic ...
        return mapped_voice, mapped_params

    def validate_environment(self, headers, model, api_key, api_base):
        api_key = api_key or get_secret_str("ELEVENLABS_API_KEY")
        headers.update({"xi-api-key": api_key, "Content-Type": "application/json"})
        return headers

    def get_complete_url(self, model, api_base, litellm_params):
        base_url = api_base or get_secret_str("ELEVENLABS_API_BASE") or self.TTS_BASE_URL
        voice_id = litellm_params.get(self.ELEVENLABS_VOICE_ID_KEY)
        return f"{base_url}{self.TTS_ENDPOINT_PATH}/{voice_id}"

    def transform_text_to_speech_request(self, model, input, voice, optional_params, litellm_params, headers):
        request_body = {"text": input, "model_id": model}
        # ... merge params ...
        return TextToSpeechRequestData(
            dict_body=request_body,
            headers={"Content-Type": "application/json"},
        )

    def transform_text_to_speech_response(self, model, raw_response, logging_obj):
        return HttpxBinaryResponseContent(raw_response)
```

### Provider Registration

**File**: `litellm/utils.py`

The `ProviderConfigManager` class has a static method for each endpoint type. The provider is registered here:

```python
class ProviderConfigManager:
    @staticmethod
    def get_provider_text_to_speech_config(
        model: str,
        provider: LlmProviders,
    ) -> Optional[BaseTextToSpeechConfig]:
        if litellm.LlmProviders.ELEVENLABS == provider:
            from litellm.llms.elevenlabs.text_to_speech.transformation import ElevenLabsTextToSpeechConfig
            return ElevenLabsTextToSpeechConfig()
        elif litellm.LlmProviders.HAMSA == provider:
            from litellm.llms.hamsa.text_to_speech.transformation import HamsaTextToSpeechConfig
            return HamsaTextToSpeechConfig()
        # ... etc
        return None

    @staticmethod
    def get_provider_audio_transcription_config(
        model: str,
        provider: LlmProviders,
    ) -> Optional[BaseAudioTranscriptionConfig]:
        # ... same pattern, different base class
```

### Provider Model Info

**File**: `litellm/llms/<provider>/common_utils.py`

Each provider has a `BaseLLMModelInfo` subclass that provides provider-level info:

```python
class HamsaModelInfo(BaseLLMModelInfo):
    def get_provider_info(self, model: str) -> Optional[ProviderSpecificModelInfo]:
        return ProviderSpecificModelInfo(
            endpoint="/v1/audio/transcriptions, /v1/realtime",
            mode="audio_transcription",
        )

    def get_models(self, api_key=None, api_base=None) -> List[str]:
        return []

    @staticmethod
    def get_api_key(api_key=None) -> Optional[str]:
        return api_key or os.environ.get("HAMSA_API_KEY")

    @staticmethod
    def get_api_base(api_base=None) -> Optional[str]:
        return api_base or os.environ.get("HAMSA_API_BASE")
```

### Provider Enum Registration

**File**: `litellm/types/utils.py`

```python
class LlmProviders(Enum):
    # ... existing providers
    HAMSA = "hamsa"
```

### Model Name Resolution

**File**: `litellm/litellm_core_utils/get_llm_provider_logic.py`

When a client sends `model=hamsa/hamsa-tts`, the `get_llm_provider()` function splits on `/`:
- `custom_llm_provider = "hamsa"` (prefix before `/`)
- `model = "hamsa-tts"` (remainder after `/`)

This is how LiteLLM knows which provider config to use.

## Complete Checklist: Adding a New Endpoint

To add a new first-class endpoint (e.g. `/v1/audio/voices`), these files must be modified:

### 1. Types layer (`litellm/types/utils.py`)
- [ ] Add `CallTypes.acreate_voice = "acreate_voice"` to the `CallTypes` enum
- [ ] Add `"acreate_voice"` to the `CallTypesLiteral` Literal type
- [ ] Add `"/v1/audio/voices": [CallTypes.acreate_voice]` to `API_ROUTE_TO_CALL_TYPES`
- [ ] Add provider to `LlmProviders` enum if not already there (e.g. `HAMSA = "hamsa"`)

### 2. Provider config layer (`litellm/llms/<provider>/`)
- [ ] Create `litellm/llms/<provider>/voice/transformation.py` with a config class
- [ ] Create `litellm/llms/<provider>/voice/__init__.py` with exports
- [ ] The config class must implement the base interface (see below)

### 3. Provider registration (`litellm/utils.py`)
- [ ] Add `HAMSA` case to `ProviderConfigManager.get_provider_voice_config()` returning the new config

### 4. SDK function (`litellm/main.py`)
- [ ] Add `async def acreate_voice()` async wrapper
- [ ] Add `def create_voice()` sync function with provider dispatch

### 5. Router methods (`litellm/router.py`)
- [ ] Add `async def acreate_voice()` public method (with fallbacks)
- [ ] Add `async def _acreate_voice()` private method (actual call)

### 6. Route dispatcher (`litellm/proxy/route_llm_request.py`)
- [ ] Add `"acreate_voice"` to the `route_type` Literal in `route_request()`
- [ ] Add `"acreate_voice": "/audio/voices"` to `ROUTE_ENDPOINT_MAPPING`

### 7. Proxy route (`litellm/proxy/proxy_server.py`)
- [ ] Add `@router.post("/v1/audio/voices")` and `@router.post("/audio/voices")` decorators
- [ ] Add `async def create_voice()` handler function

## Base Config Interface Patterns

LiteLLM has different base config classes for different endpoint types. Each follows the same pattern but with type-specific abstract methods:

### Text-to-Speech (`BaseTextToSpeechConfig`)
```python
# litellm/llms/base_llm/text_to_speech/transformation.py

class BaseTextToSpeechConfig(ABC):
    @abstractmethod
    def get_supported_openai_params(self, model: str) -> list: ...

    @abstractmethod
    def map_openai_params(self, model, optional_params, voice, drop_params, kwargs) -> Tuple[Optional[str], Dict]: ...

    @abstractmethod
    def validate_environment(self, headers, model, api_key, api_base) -> dict: ...

    @abstractmethod
    def get_complete_url(self, model, api_base, litellm_params) -> str: ...

    @abstractmethod
    def transform_text_to_speech_request(self, model, input, voice, optional_params, litellm_params, headers) -> TextToSpeechRequestData: ...

    @abstractmethod
    def transform_text_to_speech_response(self, model, raw_response, logging_obj) -> HttpxBinaryResponseContent: ...
```

### Audio Transcription (`BaseAudioTranscriptionConfig`)
```python
# litellm/llms/base_llm/audio_transcription/transformation.py

class BaseAudioTranscriptionConfig(ABC):
    @abstractmethod
    def get_supported_openai_params(self, model: str) -> list: ...

    @abstractmethod
    def map_openai_params(self, non_default_params, optional_params, model, drop_params) -> dict: ...

    @abstractmethod
    def validate_environment(self, headers, model, messages, optional_params, litellm_params, api_key, api_base) -> dict: ...

    @abstractmethod
    def get_complete_url(self, api_base, api_key, model, optional_params, litellm_params, stream) -> str: ...

    @abstractmethod
    def transform_audio_transcription_request(self, model, audio_file, optional_params, litellm_params) -> AudioTranscriptionRequestData: ...

    @abstractmethod
    def transform_audio_transcription_response(self, raw_response) -> TranscriptionResponse: ...
```

## Existing Hamsa Provider Implementation

The Hamsa provider is already implemented for STT (audio transcription) and realtime. The file structure is:

```
litellm/llms/hamsa/
    __init__.py                          # exports HamsaModelInfo
    common_utils.py                      # HamsaModelInfo(BaseLLMModelInfo)
    transcription/
        __init__.py                      # exports HamsaAudioTranscriptionConfig
        transformation.py               # HamsaAudioTranscriptionConfig
    realtime/
        __init__.py                      # exports HamsaRealtimeConfig
        handler.py                       # HamsaRealtimeConfig + hamsa_realtime()
```

To add TTS, the structure would be:

```
litellm/llms/hamsa/
    text_to_speech/
        __init__.py
        transformation.py               # HamsaTextToSpeechConfig
```

## Summary of Data Flow for a TTS Request

```
Client sends:
  POST /v1/audio/speech
  {"model": "hamsa/hamsa-tts", "input": "hello", "voice": "jasem"}

1. proxy_server.py audio_speech()
   -> parses JSON body
   -> add_litellm_data_to_request() injects api_key, api_base from model config
   -> route_request(data, route_type="aspeech")

2. route_llm_request.py route_request()
   -> getattr(llm_router, "aspeech")(**data)

3. router.py Router.aspeech()
   -> async_function_with_fallbacks() handles fallbacks/retries
   -> Router._aspeech()
      -> async_get_available_deployment() picks a deployment
      -> deployment["litellm_params"] has api_key, api_base, model
      -> litellm.aspeech(**data, input=input, voice=voice)

4. main.py aspeech()
   -> runs speech() in executor

5. main.py speech()
   -> get_llm_provider("hamsa/hamsa-tts") -> provider="hamsa", model="hamsa-tts"
   -> ProviderConfigManager.get_provider_text_to_speech_config(provider=HAMSA)
      -> returns HamsaTextToSpeechConfig()
   -> config.map_openai_params() maps voice="jasem" -> speaker="jasem"
   -> dispatches to elif custom_llm_provider == "hamsa" branch
   -> calls base_llm_http_handler.text_to_speech_handler()

6. llm_http_handler.py text_to_speech_handler()
   -> config.validate_environment() sets x-api-key header
   -> config.get_complete_url() returns "http://hamsa:8080/tts/stream"
   -> config.transform_text_to_speech_request() builds JSON body
   -> httpx.post(url, json=body, headers=headers)
   -> config.transform_text_to_speech_response() wraps as HttpxBinaryResponseContent

7. proxy_server.py audio_speech()
   -> StreamingResponse(_audio_speech_chunk_generator(response))
   -> streams WAV bytes back to client
```
