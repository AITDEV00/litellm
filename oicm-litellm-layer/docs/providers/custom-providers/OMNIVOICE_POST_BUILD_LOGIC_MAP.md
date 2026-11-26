# OmniVoice Implementation: Logic Map (Post-Build Trace)

> **Scope**: End-to-end trace of every OmniVoice code path through the LiteLLM
> 7-layer architecture, comparing against Hamsa and Inception. Identifies dead
> code, un-wired routes, and expandability gaps.
>
> **Method**: Logic mapping technique from
> `oicm-litellm-layer/docs/techniques/logic_mapping_technique.md`. Every function in every
> call chain is traced with file:line references. Dead branches are marked.

## 1. Provider Registration

| Provider | LlmProviders enum | Registered in constants? | In _CUSTOM_AUDIO_HANDLER_PROVIDERS? |
|----------|-------------------|--------------------------|--------------------------------------|
| Hamsa | `HAMSA` (`types/utils.py:3384`) | Yes | No (uses own branch in `speech()`) |
| Inception | `INCEPTION` (`types/utils.py:3298`) | Yes | Yes (`main.py:7733`) |
| OmniVoice | `OMNIVOICE` (`types/utils.py:3385`) | Yes | Yes (`main.py:7733`) |

All three are registered. No gap.

## 2. Config Class Inventory

| Config class | File | Base classes | Wired into dispatch? |
|-------------|------|-------------|---------------------|
| `OmniVoiceModelInfo` | `common_utils.py:96` | `BaseLLMModelInfo` | N/A (base for others) |
| `OmniVoiceTextToSpeechConfig` | `text_to_speech/transformation.py:36` | `OmniVoiceModelInfo, BaseTextToSpeechConfig` | Yes (`utils.py:8971`) |
| `OmniVoiceVoiceCloneConfig` | `voice/transformation.py:67` | `OmniVoiceModelInfo, BaseTextToSpeechConfig` | Yes (runtime switch in `main.py:8218`) |
| `OmniVoiceVoiceConfig` | `voice/transformation.py:171` | `OmniVoiceModelInfo, BaseVoiceConfig` | Yes (`get_provider_voice_config` dispatches OMNIVOICE) |
| `OmniVoiceScriptConfig` | `script/transformation.py:22` | `OmniVoiceModelInfo, BaseTextToSpeechConfig` | Yes (`script()`/`ascript()` SDK functions) |
| `HamsaTextToSpeechConfig` | `hamsa/text_to_speech/transformation.py:12` | `HamsaModelInfo, BaseTextToSpeechConfig` | Yes (`utils.py:8957`) |
| `HamsaVoiceConfig` | `hamsa/voice/transformation.py:13` | `HamsaModelInfo, BaseVoiceConfig` | Yes (`utils.py:9030`) |
| `InceptionTextToSpeechConfig` | `inception/text_to_speech/transformation.py:30` | `InceptionAudioModelInfo, BaseTextToSpeechConfig` | Yes (`utils.py:8964`) |

## 3. Call Chain Traces

### Route 1: Standard TTS (`POST /v1/audio/speech`)

```
User request (JSON body: model, input, voice)
    |
    v
[proxy_server.py:9012] audio_speech()
    |  route_type="aspeech"
    v
[route_llm_request.py] route_request() -> getattr(llm_router, "aspeech")(**data)
    |
    v
[router.py:3751] Router.aspeech()
    |
    v
[router.py:3803] Router._aspeech() -> litellm.speech()
    |
    v
[main.py:7735] speech()
    |  custom_llm_provider == "omnivoice" (line 8214)
    |  No ref_audio -> OmniVoiceTextToSpeechConfig (line 8226)
    v
[llm_http_handler.py:11103] text_to_speech_handler()
    |  request_data has "dict_body" -> client.post(json=dict_body)
    v
[provider] OmniVoice /v1/audio/speech
    |
    v
[response] HttpxBinaryResponseContent -> StreamingResponse
```

**Status**: Fully wired and functional. All 7 layers connected.

**Code path in transformation.py**:
- `get_complete_url()` -> strips `/v1`, appends `/v1/audio/speech`
- `map_openai_params()` -> resolves voice via `_resolve_voice()`, collects `_TTS_FORM_KEYS`, strips `instructions`, merges `extra_body`, `_collect_passthrough`
- `transform_text_to_speech_request()` -> builds `dict_body` with `model`, `input`, `voice`, form keys, passthrough
- `transform_text_to_speech_response()` -> `HttpxBinaryResponseContent(raw_response)`

### Route 2: Voice Clone TTS (`POST /v1/audio/speech/clone`)

```
User request (multipart form-data: text, ref_audio file)
    |
    v
[proxy_server.py:9133] audio_speech_clone()
    |  Reads ref_audio as (filename, bytes, content_type) tuple
    |  Sets data["ref_audio"] = tuple, data["input"] = text, data["voice"] = "clone"
    |  route_type="aspeech"
    v
[route_llm_request.py] route_request() -> getattr(llm_router, "aspeech")(**data)
    |
    v
[router.py:3751] Router.aspeech()
    |
    v
[main.py:7735] speech()
    |  custom_llm_provider == "omnivoice" (line 8214)
    |  kwargs["ref_audio"] is not None -> OmniVoiceVoiceCloneConfig (line 8218)
    v
[llm_http_handler.py:11103] text_to_speech_handler()
    |  request_data has "form_data" + "files" -> client.post(data=form_data, files=files)
    v
[provider] OmniVoice /v1/audio/speech/clone
    |
    v
[response] HttpxBinaryResponseContent -> StreamingResponse
```

**Status**: Fully wired and functional.

**Code path in voice/transformation.py**:
- `get_complete_url()` -> strips `/v1`, appends `/v1/audio/speech/clone`
- `map_openai_params()` -> collects `_CLONE_FORM_KEYS`, strips `instructions`, merges `extra_body`, `_collect_passthrough`
- `transform_text_to_speech_request()` -> pops `ref_audio` (required, raises if None), pops `ref_text`, builds `form_fields` dict, builds `files` via `_build_ref_audio_files()`
- Returns `TextToSpeechRequestData(form_data=form_fields, files=files)` (no `method` field, defaults to POST)

### Route 3: Multi-Speaker Script (`POST /v1/audio/script`) — DEAD CODE

```
OmniVoiceScriptConfig exists but is never invoked:

    [route_llm_request.py:81]  ROUTE_ENDPOINT_MAPPING["ascript"] = "/audio/script"  <-- registered
    [route_llm_request.py:274] "ascript" in valid route_types                     <-- registered
              |
              v
    route_request() dispatches: getattr(llm_router, "ascript")(**data)
              |
              v
    [router.py] Router has NO ascript method  <-- AttributeError
              |
              v
    (falls through to getattr(litellm, "ascript")(**data))
              |
              v
    [main.py] litellm has NO ascript function  <-- AttributeError
              |
              v
    DEAD END
```

**Status**: Dead code. `OmniVoiceScriptConfig` is fully implemented and its
`transform_text_to_speech_request` returns `dict_body` (compatible with
`text_to_speech_handler`), but there is no SDK function, no router method, and
no proxy route to invoke it.

**What's missing to activate**:
1. `litellm.ascript()` / `litellm.script()` function in `main.py`
2. `Router.ascript()` method in `router.py`
3. `@router.post("/v1/audio/script")` handler in `proxy_server.py`
4. `get_provider_script_config()` in `utils.py` (or reuse `get_provider_text_to_speech_config` with a type check)

### Route 4: Voice Profile CRUD — DEAD CODE

```
OmniVoiceVoiceConfig exists but is never invoked:

    [route_llm_request.py:75-80]  6 route types registered in ROUTE_ENDPOINT_MAPPING
    [route_llm_request.py:268-273] 6 route types in valid route_types
    [route_llm_request.py:580-585] 6 route types in no-model-required list
              |
              v
    route_request() dispatches: getattr(llm_router, "alist_voices")(**data)
              |
              v
    [router.py] Router has NO alist_voices method  <-- AttributeError
              |
              v
    DEAD END

Even if router methods existed:

    [main.py:8340] create_voice() calls get_provider_voice_config(provider)
              |
              v
    [utils.py:9026] get_provider_voice_config only handles HAMSA  <-- returns None for OMNIVOICE
              |
              v
    [main.py:8341] "Voice management is not supported for provider={}. Only 'hamsa'..."
              |
              v
    DEAD END
```

**Status**: Dead code. `OmniVoiceVoiceConfig` is fully implemented with 6
actions (list, list_profiles, get_profile, create_profile, update_profile,
delete_profile), but:

1. `get_provider_voice_config()` in `utils.py:9026` only returns `HamsaVoiceConfig`
2. `voice_handler()` in `llm_http_handler.py:11329` only supports `dict_body` POST — it cannot handle `form_data`, `files`, `method="GET"`, `method="DELETE"`, or `method="PATCH"`
3. No SDK functions (`alist_voices`, `acreate_voice_profile`, etc.) in `main.py`
4. No router methods in `router.py`
5. No proxy route handlers in `proxy_server.py`
6. `TextToSpeechRequestData` TypedDict has no `method` field declared

**The `method` field gap**: `OmniVoiceVoiceConfig.transform_create_voice_request()`
returns `TextToSpeechRequestData(method="GET")`, `method="DELETE"`, and
`method="PATCH"`. Since `TextToSpeechRequestData` is `total=False`, Python
silently allows extra keys. But neither `voice_handler` nor
`text_to_speech_handler` reads `method` — both hardcode `client.post()`.

## 4. Provider Comparison Matrix

| Feature | Hamsa | Inception | OmniVoice |
|---------|-------|-----------|-----------|
| **TTS** | | | |
| Config class | `HamsaTextToSpeechConfig` | `InceptionTextToSpeechConfig` | `OmniVoiceTextToSpeechConfig` |
| SDK function | `speech()` branch | `speech()` branch | `speech()` branch |
| Proxy route | `POST /v1/audio/speech` | `POST /v1/audio/speech` | `POST /v1/audio/speech` |
| HTTP handler | `text_to_speech_handler` | `text_to_speech_handler` | `text_to_speech_handler` |
| Body type | `dict_body` | `dict_body` | `dict_body` |
| **Voice clone TTS** | | | |
| Config class | N/A | N/A | `OmniVoiceVoiceCloneConfig` |
| Proxy route | N/A | N/A | `POST /v1/audio/speech/clone` |
| HTTP handler | N/A | N/A | `text_to_speech_handler` |
| Body type | N/A | N/A | `form_data` + `files` |
| **Voice management** | | | |
| Config class | `HamsaVoiceConfig` | N/A | `OmniVoiceVoiceConfig` (dead) |
| SDK function | `create_voice()` | N/A | Blocked by dispatch |
| Proxy route | `POST /v1/audio/voices` | N/A | None |
| HTTP handler | `voice_handler` (dict_body only) | N/A | `voice_handler` (incompatible) |
| Body type | `dict_body` | N/A | `form_data`+`files` / `method` field |
| **Script synthesis** | | | |
| Config class | N/A | N/A | `OmniVoiceScriptConfig` (dead) |
| SDK function | N/A | N/A | None |
| Proxy route | N/A | N/A | None |
| **STT** | | | |
| Config class | `HamsaAudioTranscriptionConfig` | `InceptionAudioTranscriptionConfig` | N/A |
| **Auth** | | | |
| API key required | Yes (`x-api-key` header) | No | No |
| `_inject_auth_headers` | Yes | No | No |
| **Code quality** | | | |
| `Optional[X]` vs `X \| None` | Old style (`Optional`) | Old style (`Optional`) | Modern (`X \| None`) |
| `_collect_passthrough` pattern | Inline loops | Mutating `(source, dest) -> None` | Non-mutating `(source) -> dict` |
| Internal params frozenset | Includes provider-specific params | Includes `secret_fields`, etc. | Most comprehensive |
| `validate_environment` | Injects auth headers | No-op (returns headers) | No-op (returns headers) |

## 5. Code Quality Comparison

### Shared patterns (all three providers)

All three follow the same structural pattern:
- `common_utils.py` with `XModelInfo(BaseLLMModelInfo)` + `X_INTERNAL_PARAMS` frozenset
- `text_to_speech/transformation.py` with `XTextToSpeechConfig(XModelInfo, BaseTextToSpeechConfig)`
- `_resolve_base()` static method that raises `BaseLLMException` if api_base is None
- `get_complete_url()` strips trailing `/v1` then appends provider-specific path
- `map_openai_params()` pops known keys, strips `instructions`, merges `extra_body`
- `transform_text_to_speech_response()` returns `HttpxBinaryResponseContent`

### Where OmniVoice is cleaner

1. **Modern type hints**: `X | None` instead of `Optional[X]` (Hamsa and Inception still use `Optional`)
2. **Non-mutating `_collect_passthrough`**: Returns a new dict instead of mutating a `dest` parameter. Inception still uses the mutating `(source, dest) -> None` pattern
3. **Derived constants**: `_PROFILE_FORM_KEYS` derived from `_CLONE_FORM_KEYS + ("ref_text",)` instead of duplicating
4. **Extracted helpers**: `_require_profile_id`, `_build_ref_audio_files`, `_resolve_voice` reduce duplication
5. **No `os.environ` coupling**: `get_api_key` returns `"no-api-key-required"` (like Inception) without env var lookup. Hamsa couples to `HAMSA_API_KEY` env var
6. **No redundant checks**: OmniVoice's `_collect_passthrough` doesn't re-check `extra_body`/`extra_headers` (they're in `OMNIVOICE_INTERNAL_PARAMS`). Inception's version redundantly checks both

### Where OmniVoice is worse (items 1-2 now resolved)

1. ~~**Dead code**: 2 of 4 config classes (`OmniVoiceVoiceConfig`, `OmniVoiceScriptConfig`) are never invoked in production~~ All 4 config classes are now invoked in production. Hamsa has zero dead config classes
2. ~~**`method` field on TypedDict**: `OmniVoiceVoiceConfig` sets `method="GET"` etc. on `TextToSpeechRequestData` but that field doesn't exist in the TypedDict definition~~ `method`, `form_data`, `files` fields added to `TextToSpeechRequestData`
3. **Inconsistent body types across configs**: TTS uses `dict_body`, clone uses `form_data`+`files`, script uses `dict_body`, voice profiles use `form_data`+`files`+`method`. Hamsa is simpler: everything is `dict_body`
4. **`OmniVoiceVoiceCloneConfig` extends `BaseTextToSpeechConfig`**: It lives in `voice/transformation.py` but extends the TTS base class, not `BaseVoiceConfig`. This is correct (it goes through `text_to_speech_handler`, not `voice_handler`) but the file naming is misleading

## 6. Expandability Assessment

### What works today (2 of 7 routes)

| Route | Expandable? | Why |
|-------|-------------|-----|
| `POST /v1/audio/speech` | Yes | Follows the standard `aspeech` -> `Router.aspeech` -> `speech()` -> `text_to_speech_handler` chain. Adding new OmniVoice TTS params is a one-liner in `_TTS_FORM_KEYS` |
| `POST /v1/audio/speech/clone` | Yes | Same chain. Adding clone-specific params is a one-liner in `_CLONE_FORM_KEYS` |

### Previously dead routes (now ACTIVATED)

All 5 dead routes are now fully wired through all 6 layers. Verified with live proxy
on localhost: the routing chain reaches `OmniVoiceVoiceConfig.get_complete_url()`
and constructs the correct upstream URLs:

- `http://<api_base>/v1/voices` (list voices)
- `http://<api_base>/v1/voices/profiles/<profile_id>` (get/update/delete profile)
- `http://<api_base>/v1/audio/script` (script endpoint)

| Route | Status | Implementation |
|-------|--------|---------------|
| `POST /v1/audio/script` | Activated | SDK `script`/`ascript`, Router `ascript`/`_ascript`, proxy `audio_script()` |
| `GET /v1/voices` | Activated | Router delegates to `acreate_voice(action="list")`, proxy `_route_voice_management()` |
| `GET /v1/voices/profiles` | Activated | Router delegates to `acreate_voice(action="list_profiles")` |
| `POST /v1/voices/profiles` | Activated | Router delegates to `acreate_voice(action="create_profile")` |
| `PATCH /v1/voices/profiles/{id}` | Activated | Router delegates to `acreate_voice(action="update_profile")` |
| `DELETE /v1/voices/profiles/{id}` | Activated | Router delegates to `acreate_voice(action="delete_profile")` |
| `GET /v1/voices/profiles/{id}` | Activated | Router delegates to `acreate_voice(action="get_profile")` |

### Expandability blockers (in order of dependency)

To activate the 5 dead routes, these changes are needed in this order:

**Layer 1: `TextToSpeechRequestData` schema** (`base_llm/text_to_speech/transformation.py:24`)
Add `method: str` field to the TypedDict so the contract is explicit.

**Layer 2: `voice_handler`** (`llm_http_handler.py:11329`)
Add support for `form_data`+`files`, and read `method` to dispatch GET/POST/PATCH/DELETE instead of hardcoding `client.post()`.

**Layer 3: `get_provider_voice_config`** (`utils.py:9026`)
Change `Literal["hamsa"]` to `Literal["hamsa", "omnivoice"]` and add the OMNIVOICE branch.

**Layer 4: SDK functions** (`main.py`)
Add `ascript()`, `alist_voices()`, `acreate_voice_profile()`, `aupdate_voice_profile()`, `aget_voice_profile()`, `adelete_voice_profile()`.

**Layer 5: Router methods** (`router.py`)
Add corresponding `Router.ascript()`, `Router.alist_voices()`, etc.

**Layer 6: Proxy routes** (`proxy_server.py`)
Add `@router.post("/v1/audio/script")`, `@router.get("/v1/voices")`, etc.

Layers 1-3 are infrastructure changes. Layers 4-6 are mechanical additions that follow the existing `aspeech`/`acreate_voice` patterns.

### Comparison: How Hamsa voice management works vs what OmniVoice needs

Hamsa's `HamsaVoiceConfig` is simpler because it only does `dict_body` POST:
- `transform_create_voice_request` returns `TextToSpeechRequestData(dict_body=body)`
- `voice_handler` handles `dict_body` POST -> works

OmniVoice's `OmniVoiceVoiceConfig` needs `form_data`+`files` (for create_profile with ref_audio) and GET/DELETE/PATCH (for profile CRUD):
- `transform_create_voice_request` returns `TextToSpeechRequestData(form_data=..., files=..., method="PATCH")`
- `voice_handler` only handles `dict_body` POST -> broken

Hamsa's voice management is a single POST endpoint (`/tts/voice_clone` or `/tts/load_voice_cloning`). OmniVoice's is a full CRUD REST API with 6 endpoints. The `voice_handler` was designed for Hamsa's simple POST-only model and cannot handle OmniVoice's richer API surface.

## 7. Summary

### Clean aspects
- TTS and voice clone routes are fully wired, tested, and follow the same architecture as Inception and Hamsa
- Config classes are well-structured with extracted helpers, derived constants, and modern type hints
- `_collect_passthrough` is non-mutating (better than Inception's mutating version)
- Internal params frozenset is comprehensive

### Gaps (all resolved)
- ~~2 of 4 config classes are dead code~~ All 4 config classes are now invoked in production
- ~~`voice_handler` only supports `dict_body` POST~~ `voice_handler`/`async_voice_handler` now dispatch on body type and HTTP method via `_send_voice_request_sync`/`_send_voice_request_async` helpers
- ~~`TextToSpeechRequestData` has no `method` field~~ Added `method`, `form_data`, `files` fields
- ~~`get_provider_voice_config` only dispatches HAMSA~~ Now dispatches HAMSA and OMNIVOICE
- ~~5 route types registered but no SDK functions/router methods/proxy routes~~ All 5 route types now have full SDK/router/proxy wiring
- ~~No `ascript` SDK function~~ `script`/`ascript` SDK functions added

### Is the implementation "clean like Hamsa and Inception"?

**For the 2 working routes**: Yes. The TTS and voice clone configs follow the same pattern as Hamsa and Inception, with some improvements (modern types, non-mutating helpers, derived constants).

**For the 5 previously dead routes**: All infrastructure work is complete. The config classes are now fully reachable through the standard 7-layer architecture.

### Are the new routes expandable?

**All 7 routes are easily expandable**: Adding new form params is a one-liner in the `_TTS_FORM_KEYS` / `_CLONE_FORM_KEYS` / `_SCRIPT_FORM_KEYS` tuples. The `voice_handler` now supports all HTTP methods and body types, so new voice management actions can be added without further infrastructure changes.
