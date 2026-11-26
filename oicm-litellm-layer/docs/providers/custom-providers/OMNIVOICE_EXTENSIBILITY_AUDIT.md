# OmniVoice Extensibility Audit

> Scope: Logic map of every OmniVoice code path with file:line references,
> focused on hardcoded strings, non-extensible patterns, and code smells that
> would break if a second audio provider (or even a second OmniVoice deployment
> with different configuration) were added.

## 1. Complete Call Chain Map

### Route 1: Standard TTS (`POST /v1/audio/speech`)

```
[proxy_server.py:9012]  audio_speech()
    |  route_type="aspeech"
    v
[route_llm_request.py:73]  ROUTE_ENDPOINT_MAPPING["aspeech"] = "/audio/speech"
[route_llm_request.py:560-585]  route_request() -> getattr(llm_router, "aspeech")(**data)
    |
    v
[router.py:3751]  Router.aspeech()  ->  async_function_with_fallbacks()
    |
    v
[router.py:3803]  Router._aspeech()  ->  litellm.aspeech()
    |
    v
[main.py:7693]  aspeech()  ->  speech()
    |
    v
[main.py:7735]  speech()
    |  ProviderConfigManager.get_provider_text_to_speech_config(provider)  [utils.py:8921]
    |  custom_llm_provider == "omnivoice"  [main.py:8217]
    |  No ref_audio -> OmniVoiceTextToSpeechConfig  [main.py:8228]
    v
[llm_http_handler.py:11103]  text_to_speech_handler()  ->  client.post(json=dict_body)
    |
    v
[text_to_speech/transformation.py]
    get_complete_url()  ->  strip /v1, append /v1/audio/speech
    map_openai_params() ->  _resolve_voice(), _TTS_FORM_KEYS, _collect_passthrough
    transform_text_to_speech_request()  ->  dict_body
    transform_text_to_speech_response() ->  HttpxBinaryResponseContent
```

### Route 2: Voice Clone TTS (`POST /v1/audio/speech/clone`)

```
[proxy_server.py:9133]  audio_speech_clone()
    |  Reads ref_audio as (filename, bytes, content_type) tuple
    |  data["ref_audio"] = tuple, data["input"] = text, data["voice"] = "clone"
    |  data["model"] = _resolve_voice_management_model(llm_router)  [HARDCODED]
    |  route_type="aspeech"
    v
[route_llm_request.py]  route_request() -> getattr(llm_router, "aspeech")(**data)
    |
    v
[router.py:3751]  Router.aspeech() -> Router._aspeech() -> litellm.aspeech() -> speech()
    |
    v
[main.py:8217]  speech()
    |  custom_llm_provider == "omnivoice"
    |  kwargs["ref_audio"] is not None -> OmniVoiceVoiceCloneConfig  [main.py:8219]
    v
[llm_http_handler.py:11103]  text_to_speech_handler()  ->  client.post(data=form_data, files=files)
    |
    v
[voice/transformation.py]
    get_complete_url()  ->  strip /v1, append /v1/audio/speech/clone
    transform_text_to_speech_request()  ->  form_data + files via _build_ref_audio_files()
```

### Route 3: Script Synthesis (`POST /v1/audio/script`)

```
[proxy_server.py:9678]  audio_script()
    |  data["model"] = _resolve_voice_management_model(llm_router)  [HARDCODED]
    |  Pops script, default_voice, speed, response_format, output_format,
    |    pause_between_speakers, on_error from data
    |  route_type="ascript"
    v
[route_llm_request.py:81]  ROUTE_ENDPOINT_MAPPING["ascript"] = "/audio/script"
[route_llm_request.py:560-585]  route_request() -> getattr(llm_router, "ascript")(**data)
    |
    v
[router.py:3958]  Router.ascript() -> async_function_with_fallbacks()
    |
    v
[router.py:3978]  Router._ascript() -> litellm.ascript()
    |
    v
[main.py:8387]  ascript() -> script()
    |
    v
[main.py:8411]  script()
    |  HARDCODED IMPORT: from litellm.llms.omnivoice.script.transformation import OmniVoiceScriptConfig
    |  text_to_speech_provider_config = OmniVoiceScriptConfig()  [HARDCODED - no provider dispatch]
    v
[llm_http_handler.py:11103]  text_to_speech_handler()  ->  client.post(json=dict_body)
    |
    v
[script/transformation.py]
    get_complete_url()  ->  strip /v1, append /v1/audio/script
    map_openai_params() ->  extract script/speakers from kwargs
    transform_text_to_speech_request()  ->  dict_body with model/script/speakers
    transform_text_to_speech_response() ->  JSON dict or HttpxBinaryResponseContent
```

### Route 4-9: Voice Profile CRUD (`GET/POST/PATCH/DELETE /v1/voices/...`)

All 6 endpoints delegate to a single shared handler:

```
[proxy_server.py:9530-9660]  list_voices / list_voice_profiles / get_voice_profile
                             create_voice_profile / update_voice_profile / delete_voice_profile
    |  Each calls _route_voice_management(action=..., route_type=...)
    v
[proxy_server.py:9435]  _route_voice_management()
    |  model = _resolve_voice_management_model(llm_router)  [HARDCODED]
    |  voice_data = {k: data.pop(k) for k in _VOICE_DATA_KEYS}
    |  voice_data["action"] = action
    |  route_type = "alist_voices" / "alist_voice_profiles" / "aget_voice_profile" /
    |               "acreate_voice_profile" / "aupdate_voice_profile" / "adelete_voice_profile"
    v
[route_llm_request.py:75-80]  ROUTE_ENDPOINT_MAPPING for 6 voice route types
[route_llm_request.py:560-585]  route_request() -> getattr(llm_router, route_type)(**data)
    |
    v
[router.py:3940-3956]  All 6 Router methods delegate to Router.acreate_voice()
    |
    v
[router.py:3865]  Router.acreate_voice() -> async_function_with_fallbacks()
    |
    v
[router.py:3884]  Router._acreate_voice() -> litellm.acreate_voice()
    |
    v
[main.py:8264]  acreate_voice() -> create_voice()
    |
    v
[main.py:8289]  create_voice()
    |  litellm_params_dict["voice_action"] = voice_data.get("action", "register")
    |  ProviderConfigManager.get_provider_voice_config(provider)  [utils.py:9026]
    |  -> OmniVoiceVoiceConfig
    v
[llm_http_handler.py:11384]  voice_handler() -> async_voice_handler()
    |  validate_environment, get_complete_url, transform_create_voice_request
    |  _send_voice_request_sync/async (dispatches on method: GET/POST/PATCH/DELETE)
    v
[voice/transformation.py]
    get_complete_url()  ->  routes based on voice_action in litellm_params
    transform_create_voice_request()  ->  form_data + files for create/update, empty for GET/DELETE
    transform_create_voice_response() ->  dict from JSON or empty-dict for 204
```

### Route 10: Legacy Voice Registration (`POST /v1/audio/voices`)

```
[proxy_server.py:9270]  create_voice()  [DUPLICATE of _route_voice_management pattern]
    |  Uses local voice_data_keys tuple (13 keys, subset of _VOICE_DATA_KEYS)
    |  route_type="acreate_voice"
    v
    (same chain as Route 4-9, but action defaults to "register")
```

### Routes 11-14: Direct Pod Proxy (`GET /v1/audio/models, /health, /metrics`)

```
[proxy_server.py:9820-9895]  audio_models / audio_model_detail / audio_health / audio_metrics
    |  Each calls _proxy_to_omnivoice_pod(request, user_api_key_dict, path)
    v
[proxy_server.py:9781]  _proxy_to_omnivoice_pod()
    |  api_base = _resolve_omnivoice_api_base(llm_router)  [HARDCODED]
    |  httpx.AsyncClient(verify=False, timeout=30.0)
    |  client.get(target_url)  [GET ONLY]
    v
    Direct passthrough to OmniVoice pod
```

## 2. Hardcoded / Non-Extensible Patterns

### SMELL 1: `_resolve_voice_management_model` hardcodes `"omnivoice/"` string match

**File**: `proxy_server.py:9405-9414`

```python
def _resolve_voice_management_model(router_instance: Router) -> str | None:
    for deployment in router_instance.model_list:
        litellm_params = deployment.get("litellm_params", {})
        model_str = litellm_params.get("model", "")
        if "omnivoice/" in model_str or "/omnivoice" in model_str:
            return deployment.get("model_name")
    for deployment in router_instance.model_list:
        litellm_params = deployment.get("litellm_params", {})
        if litellm_params.get("mode") == "audio_speech":
            return deployment.get("model_name")
    return None
```

**Problem**: The primary lookup hardcodes the string `"omnivoice/"`. If a second audio provider (e.g. "hamsa") is deployed, the first loop returns the OmniVoice deployment even when the caller needs the Hamsa one. The fallback loop (mode == "audio_speech") is provider-agnostic but only runs if no OmniVoice deployment exists, so it's unreachable when both are present.

**Impact**: Adding a second audio provider breaks voice management, script synthesis, and voice clone model resolution. All three endpoints (`audio_speech_clone`, `audio_script`, `_route_voice_management`) call this function.

**Fix**: Accept a `provider: str` parameter and match on `litellm_params.get("model", "").startswith(f"{provider}/")`. Or better: use the router's existing `mode` field and let the caller specify which mode to resolve.

---

### SMELL 2: `_resolve_omnivoice_api_base` has the same hardcoded match, and the function name itself is provider-specific

**File**: `proxy_server.py:9418-9430`

```python
def _resolve_omnivoice_api_base(router_instance: Router) -> str | None:
    for deployment in router_instance.model_list:
        litellm_params = deployment.get("litellm_params", {})
        model_str = litellm_params.get("model", "")
        if "omnivoice/" in model_str or "/omnivoice" in model_str:
            api_base = litellm_params.get("api_base")
            if api_base:
                return str(api_base).rstrip("/")
    ...
```

**Problem**: Same hardcoded string match as SMELL 1. Additionally, the function name embeds "omnivoice", making it semantically impossible to reuse for another provider. Called by `_proxy_to_omnivoice_pod` for the direct pod proxy routes (models, health, metrics).

**Impact**: The `/v1/audio/models`, `/v1/audio/health`, `/v1/audio/metrics` proxy endpoints will always route to OmniVoice's pod. A second audio provider's models/health/metrics endpoints are unreachable.

**Fix**: Rename to `_resolve_audio_provider_api_base(router_instance, provider: str)` and parameterize the string match.

---

### SMELL 3: `_proxy_to_omnivoice_pod` is OmniVoice-specific by name and design

**File**: `proxy_server.py:9781-9810`

```python
async def _proxy_to_omnivoice_pod(
    request: Request,
    user_api_key_dict: UserAPIKeyAuth,
    path: str,
) -> Response:
    import httpx
    ...
    api_base = _resolve_omnivoice_api_base(llm_router)
    ...
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "authorization")
        }
        resp = await client.get(target_url, headers=headers, params=dict(request.query_params))
    ...
```

**Problems**:
1. Function name hardcodes "omnivoice"
2. `verify=False` is hardcoded with no way to override per-provider
3. `timeout=30.0` is hardcoded with no configurability
4. GET-only: `client.get()` is hardcoded. No POST/PATCH/DELETE support for proxied endpoints
5. `import httpx` is a local import inside the function (minor: PLC0415 style, though in project ignore list)
6. No streaming support: reads entire response into memory via `resp.content`

**Impact**: Cannot be reused for another audio provider without copying the function and renaming it. The hardcoded TLS/timeout settings may be wrong for a different provider.

**Fix**: Parameterize provider name, accept SSL/timeout config from litellm_params, support configurable HTTP methods.

---

### SMELL 4: `script()` in main.py directly imports `OmniVoiceScriptConfig` instead of using ProviderConfigManager

**File**: `main.py:8460-8462`

```python
def script(...):
    ...
    from litellm.llms.omnivoice.script.transformation import OmniVoiceScriptConfig

    text_to_speech_provider_config = OmniVoiceScriptConfig()
```

**Problem**: Every other TTS provider config is resolved through `ProviderConfigManager.get_provider_text_to_speech_config()` which dispatches on the `LlmProviders` enum. The `script()` function bypasses this entirely and hardcodes the OmniVoice config class. This means:
1. No other provider can implement script synthesis without modifying `main.py`
2. The provider is determined by the model string at the `get_llm_provider` call, but the config is always OmniVoice regardless
3. If a user deploys `hamsa/script-model`, `script()` will still use `OmniVoiceScriptConfig`

**Impact**: Script synthesis is permanently locked to OmniVoice. Adding script support for another provider requires a code change in `main.py`.

**Fix**: Add `get_provider_script_config(provider)` to `ProviderConfigManager` (or extend `get_provider_text_to_speech_config` to return script configs when the provider supports them). Use the same dispatch pattern as `speech()`.

---

### SMELL 5: `speech()` in main.py hardcodes OmniVoice branch with inline config selection logic

**File**: `main.py:8217-8234`

```python
    elif custom_llm_provider == "omnivoice":
        if kwargs.get("ref_audio") is not None:
            from litellm.llms.omnivoice.voice.transformation import (
                OmniVoiceVoiceCloneConfig,
            )

            if text_to_speech_provider_config is None or not isinstance(
                text_to_speech_provider_config, OmniVoiceVoiceCloneConfig
            ):
                text_to_speech_provider_config = OmniVoiceVoiceCloneConfig()
        else:
            from litellm.llms.omnivoice.text_to_speech.transformation import (
                OmniVoiceTextToSpeechConfig,
            )

            if text_to_speech_provider_config is None:
                text_to_speech_provider_config = OmniVoiceTextToSpeechConfig()
```

**Problem**: The `speech()` function has a provider-specific `elif` branch that does runtime config class swapping based on `kwargs["ref_audio"]`. The `ProviderConfigManager.get_provider_text_to_speech_config()` call earlier (line 7780) already returned `OmniVoiceTextToSpeechConfig`, but this branch overrides it with `OmniVoiceVoiceCloneConfig` if `ref_audio` is present. This is a provider-specific concern leaking into the generic SDK entry point.

Note: `hamsa` and `inception` have similar `elif` branches in `speech()`, so this is an existing pattern. But the OmniVoice branch is the only one that does runtime config swapping based on a kwarg; the others just instantiate a config if the manager returned None.

**Impact**: The generic `speech()` function contains OmniVoice-specific branching logic. If OmniVoice adds a third TTS mode, another condition must be added here.

**Fix**: Move the ref_audio -> VoiceCloneConfig dispatch into the config class itself (e.g. a `get_config_for_request(kwargs)` classmethod on `OmniVoiceModelInfo`), or have `ProviderConfigManager.get_provider_text_to_speech_config` accept the full kwargs and return the right config.

---

### SMELL 6: Duplicate `voice_data_keys` tuple in `create_voice()` vs `_VOICE_DATA_KEYS` frozenset

**File**: `proxy_server.py:9294-9307` (local tuple in `create_voice()`) vs `proxy_server.py:9382-9402` (`_VOICE_DATA_KEYS` frozenset)

The `create_voice()` endpoint (Route 10, legacy voice registration) defines a local tuple of 13 keys:

```python
voice_data_keys = (
    "speaker", "speaker_id", "voice_id", "name",
    "audio_url", "audio_path", "stored_path",
    "prompt_text", "transcript", "dialect",
    "global_token_ids", "semantic_token_ids",
    "action",
)
```

The module-level `_VOICE_DATA_KEYS` frozenset (used by `_route_voice_management`) has 17 keys: the same 13 plus `ref_audio`, `ref_text`, `profile_id`, `overwrite`.

**Problem**: Two separate definitions of "voice data keys" in the same file. The local tuple is a strict subset of the frozenset. If a new voice field is added, both must be updated. The local tuple in `create_voice()` was not updated when `ref_audio`, `ref_text`, `profile_id`, `overwrite` were added to `_VOICE_DATA_KEYS`, so the legacy endpoint silently drops those fields.

**Impact**: The legacy `POST /v1/audio/voices` endpoint (Route 10) cannot pass `ref_audio`, `ref_text`, `profile_id`, or `overwrite` to the provider, while the newer `_route_voice_management` endpoints can. This is a silent functional discrepancy between two endpoints that appear to do the same thing.

**Fix**: Delete the local tuple and use `_VOICE_DATA_KEYS` everywhere. Or better: delete the `create_voice()` endpoint entirely if it's superseded by `_route_voice_management` with `action="register"`.

---

### SMELL 7: `get_provider_voice_config` uses a `Literal["hamsa", "omnivoice"]` type hint instead of `LlmProviders`

**File**: `utils.py:9026-9038`

```python
    @staticmethod
    def get_provider_voice_config(
        provider: Literal["hamsa", "omnivoice"],
    ) -> Optional[Any]:
        if litellm.LlmProviders.HAMSA == provider:
            from litellm.llms.hamsa.voice.transformation import HamsaVoiceConfig
            return HamsaVoiceConfig()
        if litellm.LlmProviders.OMNIVOICE == provider:
            from litellm.llms.omnivoice.voice/transformation import OmniVoiceVoiceConfig
            return OmniVoiceVoiceConfig()
        return None
```

**Problem**: The parameter type is `Literal["hamsa", "omnivoice"]` while every other `get_provider_*_config` method uses `LlmProviders`. This means adding a third voice provider requires updating both the Literal type and adding a branch. The return type is `Optional[Any]` instead of `Optional[BaseVoiceConfig]`.

**Impact**: Minor, but it's an inconsistency that makes the type system weaker and signals that voice management was treated as a special case rather than following the standard pattern.

**Fix**: Change parameter type to `LlmProviders`, return type to `Optional[BaseVoiceConfig]`.

---

### SMELL 8: Router voice methods are 6 thin wrappers that all delegate to `acreate_voice`

**File**: `router.py:3940-3956`

```python
    async def alist_voices(self, model: str, voice_data: dict, **kwargs):
        return await self.acreate_voice(model=model, voice_data=voice_data, **kwargs)

    async def alist_voice_profiles(self, model: str, voice_data: dict, **kwargs):
        return await self.acreate_voice(model=model, voice_data=voice_data, **kwargs)

    async def aget_voice_profile(self, model: str, voice_data: dict, **kwargs):
        return await self.acreate_voice(model=model, voice_data=voice_data, **kwargs)

    async def acreate_voice_profile(self, model: str, voice_data=voice_data, **kwargs):
        return await self.acreate_voice(model=model, voice_data=voice_data, **kwargs)

    async def aupdate_voice_profile(self, model: str, voice_data: dict, **kwargs):
        return await self.acreate_voice(model=model, voice_data=voice_data, **kwargs)

    async def adelete_voice_profile(self, model: str, voice_data: dict, **kwargs):
        return await self.acreate_voice(model=model, voice_data=voice_data, **kwargs)
```

**Problem**: 6 identical methods that do nothing but delegate. The action differentiation happens entirely in the proxy layer (`_route_voice_management` sets `voice_data["action"]`), not in the router. The router doesn't even look at the action. This means:
1. The route_type in `route_request()` is meaningless for voice management; it always resolves to the same `acreate_voice` call
2. The 6 route types registered in `ROUTE_ENDPOINT_MAPPING` exist only to satisfy the `getattr(llm_router, route_type)` dispatch
3. If a provider needs different routing for list vs create vs delete (e.g. different deployment pools), the router can't support it

**Impact**: Adding a new voice management action requires adding another thin wrapper method here, another entry in `ROUTE_ENDPOINT_MAPPING`, another entry in the route_request Literal, and another entry in the no-model-required list. That's 4 places for a single new action.

**Fix**: Collapse all 6 into a single `acreate_voice` route type. Have `_route_voice_management` use `route_type="acreate_voice"` for all actions. The action is already in `voice_data`, so the provider config can dispatch on it. Delete the 6 wrapper methods and their ROUTE_ENDPOINT_MAPPING entries.

---

### SMELL 9: `audio_speech_clone` and `audio_script` call `_resolve_voice_management_model` to find the model

**File**: `proxy_server.py:9175-9178` (clone), `proxy_server.py:9720-9722` (script)

```python
        if data.get("model") is None and llm_router is not None:
            resolved = _resolve_voice_management_model(llm_router)
            if resolved is not None:
                data["model"] = resolved
```

**Problem**: Both endpoints call a function named `_resolve_voice_management_model` to find a model for TTS and script synthesis. The function name implies it's for voice management, not TTS. This is a naming smell that obscures the actual purpose: "find the audio provider deployment."

More importantly, the function (as shown in SMELL 1) hardcodes `"omnivoice/"`. So if a user sends a clone or script request without specifying a model, the proxy auto-resolves to OmniVoice even if the user intended to use a different audio provider.

**Impact**: Model auto-resolution for clone and script is silently locked to OmniVoice.

**Fix**: Rename to `_resolve_audio_model(router_instance, provider=None)` and parameterize.

---

### SMELL 10: `_VOICE_DATA_KEYS` includes provider-specific fields like `global_token_ids` and `semantic_token_ids`

**File**: `proxy_server.py:9382-9402`

```python
_VOICE_DATA_KEYS = frozenset({
    "speaker", "speaker_id", "voice_id", "name",
    "audio_url", "audio_path", "stored_path",
    "prompt_text", "transcript", "dialect",
    "global_token_ids", "semantic_token_ids",  # OmniVoice-specific
    "action", "ref_audio", "ref_text",
    "profile_id", "overwrite",
})
```

**Problem**: `global_token_ids` and `semantic_token_ids` are OmniVoice-specific voice registration fields. They're baked into the proxy-level frozenset that applies to all providers. A different audio provider with different voice registration fields would need to either add its fields here (coupling the proxy to every provider's schema) or have them silently dropped.

**Impact**: The proxy layer is coupled to OmniVoice's voice data schema. Adding a new audio provider requires modifying this frozenset or the provider's fields are lost.

**Fix**: Move voice data key filtering into the provider config's `transform_create_voice_request()`. The proxy should pass through all non-litellm-internal keys and let the provider config decide what to use.

---

### SMELL 11: `audio_script()` hardcodes the list of script-specific fields to extract

**File**: `proxy_server.py:9727-9732`

```python
        script_kwargs: dict = {"script": script_segments}
        for key in ("default_voice", "speed", "response_format", "output_format",
                     "pause_between_speakers", "on_error"):
            value = data.pop(key, None)
            if value is not None:
                script_kwargs[key] = value
```

**Problem**: The set of script-specific fields is hardcoded in the proxy endpoint. If OmniVoice adds a new script parameter, this tuple must be updated. If another provider supports script synthesis with different parameters, this tuple is wrong.

**Impact**: Script parameter list is coupled to the proxy layer.

**Fix**: Pass all non-litellm-internal keys through and let `OmniVoiceScriptConfig.map_openai_params()` handle the field extraction (it already does this for `script` and `speakers`).

---

### SMELL 12: `_proxy_to_omnivoice_pod` strips `/v1` with a hardcoded suffix check

**File**: `proxy_server.py:9802-9806`

```python
    if api_base.lower().endswith("/v1"):
        base = api_base[:-3]
    else:
        base = api_base
    target_url = base + path
```

**Problem**: The `/v1` stripping logic is hardcoded here and also duplicated in every OmniVoice config class's `get_complete_url()` method (`_resolve_base` in `common_utils.py`). A provider that doesn't use `/v1` in its base URL would have incorrect URL construction.

**Impact**: Minor, but it's duplicated logic that should live in one place.

**Fix**: Use the provider config's `get_complete_url()` for URL construction instead of reimplementing it in the proxy.

## 3. Code Smells (Non-Extensibility)

### SMELL 13: `create_voice()` in main.py has hardcoded error message listing supported providers

**File**: `main.py:8347-8350`

```python
    if voice_provider_config is None:
        raise Exception(
            "Voice management is not supported for provider={}. Supported providers: hamsa, omnivoice.".format(
                custom_llm_provider
            )
        )
```

**Problem**: The error message hardcodes "hamsa, omnivoice". When a third voice provider is added, this message must be manually updated or it will be misleading.

**Impact**: Minor, but it's a maintenance burden.

**Fix**: Generate the list dynamically from the enum or just say "Voice management is not supported for provider={}".

---

### SMELL 14: `verify=False` hardcoded in `_proxy_to_omnivoice_pod` with no override

**File**: `proxy_server.py:9808`

```python
    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:
```

**Problem**: TLS verification is disabled with no way to enable it. This is a security concern for any provider that uses valid TLS certificates. The OmniVoice pod uses a self-signed cert, so `verify=False` is needed, but a different audio provider might have proper TLS.

**Impact**: Security risk and non-extensibility.

**Fix**: Read SSL verification setting from the deployment's litellm_params (e.g. `ssl_verify` field, which the HTTP handler already supports).

---

### SMELL 15: `_CUSTOM_AUDIO_HANDLER_PROVIDERS` frozenset in main.py

**File**: `main.py:7731`

```python
_CUSTOM_AUDIO_HANDLER_PROVIDERS: frozenset[str] = frozenset({"inception", "omnivoice"})
```

**Problem**: This frozenset determines which providers get the custom audio handler branch in `speech()` vs the OpenAI-compatible branch. Adding a new custom audio provider requires adding to this frozenset AND adding an `elif` branch in `speech()`. The check is:

```python
if custom_llm_provider == "openai" or (
    custom_llm_provider in litellm.openai_compatible_providers
    and custom_llm_provider not in _CUSTOM_AUDIO_HANDLER_PROVIDERS
):
```

So a provider in `openai_compatible_providers` that also needs custom audio handling must be excluded via this frozenset. This is an indirect coupling mechanism.

**Impact**: Adding a new custom audio provider requires touching 3 places: the enum, this frozenset, and the `elif` branch in `speech()`.

**Fix**: Instead of maintaining an exclusion set, have `ProviderConfigManager.get_provider_text_to_speech_config()` return None for OpenAI-compatible providers, and non-None for custom audio providers. Then branch on whether the config is None rather than on the frozenset membership.

## 4. Summary: What Breaks If a Second Audio Provider Is Added

| Pattern | File:Line | What breaks |
|---------|-----------|-------------|
| `_resolve_voice_management_model` hardcodes `"omnivoice/"` | `proxy_server.py:9409` | Model resolution for clone, script, and voice CRUD always picks OmniVoice |
| `_resolve_omnivoice_api_base` hardcodes `"omnivoice/"` | `proxy_server.py:9422` | Pod proxy (models/health/metrics) always routes to OmniVoice |
| `_proxy_to_omnivoice_pod` is OmniVoice-named, GET-only, verify=False | `proxy_server.py:9781` | Cannot proxy to another provider's pod without copying and renaming the function |
| `script()` hardcodes `OmniVoiceScriptConfig` import | `main.py:8460` | Script synthesis is permanently locked to OmniVoice |
| `speech()` has OmniVoice-specific `elif` with runtime config swapping | `main.py:8217` | Adding a third TTS mode for OmniVoice or any TTS mode for another provider requires modifying `speech()` |
| `_VOICE_DATA_KEYS` includes OmniVoice-specific fields | `proxy_server.py:9393-9394` | New provider's voice fields must be added here or are silently dropped |
| `audio_script()` hardcodes script field list | `proxy_server.py:9727` | New script params require proxy code changes |
| `_CUSTOM_AUDIO_HANDLER_PROVIDERS` exclusion set | `main.py:7731` | New custom audio provider must be added here AND get an `elif` branch |
| 6 thin Router wrappers that all delegate to `acreate_voice` | `router.py:3940-3956` | New voice action requires 4 places to update |
| Duplicate `voice_data_keys` tuple (13 keys) vs frozenset (17 keys) | `proxy_server.py:9294` | Legacy endpoint silently drops `ref_audio`, `ref_text`, `profile_id`, `overwrite` |
| `get_provider_voice_config` uses `Literal["hamsa", "omnivoice"]` | `utils.py:9026` | Type signature must be updated for each new voice provider |
| Error message hardcodes "hamsa, omnivoice" | `main.py:8348` | Misleading when third provider is added |

## 5. Extensibility Fix Priority

### High priority (breaks multi-provider)

1. **Parameterize `_resolve_voice_management_model` and `_resolve_omnivoice_api_base`**: Accept a `provider` parameter, match on `litellm_params.get("model", "").startswith(f"{provider}/")` instead of hardcoded string. Rename to `_resolve_audio_model` and `_resolve_audio_api_base`.

2. **Fix `script()` to use ProviderConfigManager**: Add `get_provider_script_config(provider)` to `ProviderConfigManager` or extend `get_provider_text_to_speech_config` to handle script configs.

3. **Generalize `_proxy_to_omnivoice_pod`**: Rename to `_proxy_to_audio_pod`, parameterize provider, read SSL/timeout from litellm_params, support configurable HTTP methods.

### Medium priority (couples proxy to provider schema)

4. **Move voice data key filtering to provider config**: Delete `_VOICE_DATA_KEYS` from proxy_server.py. Let the provider config's `transform_create_voice_request` handle field extraction from the full passthrough dict.

5. **Move script field extraction to provider config**: Let `OmniVoiceScriptConfig.map_openai_params` extract script-specific fields from kwargs instead of hardcoding them in `audio_script()`.

6. **Delete duplicate `voice_data_keys` tuple in `create_voice()`**: Use `_VOICE_DATA_KEYS` or delete the endpoint if superseded.

### Low priority (maintenance burden)

7. **Collapse 6 Router voice wrappers into 1**: Use `route_type="acreate_voice"` for all voice management actions.

8. **Fix `get_provider_voice_config` type signature**: Use `LlmProviders` instead of `Literal["hamsa", "omnivoice"]`.

9. **Generate error message dynamically**: Don't hardcode provider names in the error string.

10. **Replace `_CUSTOM_AUDIO_HANDLER_PROVIDERS` with config-presence check**: Branch on whether `get_provider_text_to_speech_config` returns None instead of maintaining an exclusion set.
