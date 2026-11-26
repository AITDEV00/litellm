# Drama 3 through the gateway: the three gaps, and who owns them

Status: fixed. The drama3 provider slice (litellm/llms/drama3) owns the TTS
param surface and voice management, the create_voice route parses multipart,
and the code smell detection pass (oicm-litellm-layer/docs/techniques/
code_smell_detection_technique.md) audited the slice after implementation.
Written 2026-11-03.

Drama 3 is served by an sglang-omni pod that supports voice cloning, persistent
voices, streaming, batch synthesis, and full sampling control. Through the LiteLLM
gateway only default-voice non-streaming TTS and inline direction tags reach it.
Three separate defects cause that, and all three are in this fork, not upstream.

## Ownership summary

| Defect | Layer | Upstream or ours |
| ------ | ----- | ---------------- |
| `references`, `stream`, `top_k`, `seed`, `language` dropped from TTS bodies | `litellm/main.py` `speech()` optional-params assembly | Upstream design, we just have no provider config to bypass it |
| `POST /v1/audio/voices` multipart returns 500 | `litellm/proxy/voice_routes.py` `create_voice` | **Ours**, this route does not exist upstream |
| `POST /v1/audio/voices` JSON returns 400 "not supported for provider=hosted_vllm" | `litellm/endpoints/voice/main.py` | **Ours**, `base_llm/voice/` does not exist upstream |

Upstream `main` has no `litellm/proxy/voice_routes.py` and no
`litellm/llms/base_llm/voice/`, verified with
`git ls-tree -r --name-only upstream/main | grep -E "voice"`. Every voice-management
route in this fork is local code.

## Defect 1: the TTS param whitelist

`litellm/main.py` `speech()` builds the outbound body from exactly three optional
parameters:

```python
optional_params = {}
if response_format is not None:
    optional_params["response_format"] = response_format
if speed is not None:
    optional_params["speed"] = speed
if instructions is not None:
    optional_params["instructions"] = instructions
```

Anything else you send is accepted, logged, and never forwarded. This is upstream
behaviour, not a fork regression: `upstream/main` has the same three lines at
`litellm/main.py:9017`.

What routes around it is `ProviderConfigManager.get_provider_text_to_speech_config`,
which returns a `BaseTextToSpeechConfig` per provider. When a config is returned,
`speech()` dispatches to it instead of the OpenAI branch, and the config owns
`map_openai_params`, so it can forward whatever the upstream server accepts. This
fork already uses that mechanism for OmniVoice, which forwards an arbitrary body
via `_collect_passthrough`.

`hosted_vllm` has no entry in that dispatch table, so Drama 3 falls through to the
OpenAI branch and inherits its whitelist.

## Defect 2: the multipart 500

`litellm/proxy/voice_routes.py` `create_voice` reads the body twice:

```python
body = await request.body()
data = orjson.loads(body)
```

A multipart upload cannot be parsed as JSON, so the request fails with
`RuntimeError: Stream consumed` and returns HTTP 500. The pod's own
`POST /v1/audio/voices` is multipart by contract, so the two can never meet.

## Defect 3: the JSON path rejects the provider

Sending JSON instead reaches a different wall, in
`litellm/endpoints/voice/main.py:117`:

```
litellm.BadRequestError: Voice management is not supported for provider=hosted_vllm
```

The voice subsystem dispatches through `get_provider_voice_config`, which returns a
`BaseVoiceConfig` for `HAMSA` and `OMNIVOICE` only. `hosted_vllm` returns `None`.

So the gateway has two voice routers that both refuse this model, for different
reasons.

## Why a custom provider is the right fix

The three defects have one shared shape: the gateway has a generic extension point
per capability, and `hosted_vllm` is not registered in it. Drama 3 is not a
hosted_vllm model in any meaningful sense. It is an sglang-omni TTS server with its
own richer contract that happens to speak `/v1/audio/speech`.

Registering it as `hosted_vllm` was the reason the controller misclassified it as
`chat` before the tier-0 fix, and it is the reason its body gets rewritten now.

### Proposed shape

A `drama3` provider, following the existing `oicm_providers` slice:

```
litellm/llms/drama3/
  text_to_speech/transformation.py   Drama3TextToSpeechConfig
  voice/transformation.py            Drama3VoiceConfig
```

`Drama3TextToSpeechConfig.map_openai_params` forwards the nine fields the pod
accepts (`input` stays in the body; `references`, `top_k`, `top_p`, `temperature`,
`max_new_tokens`, `stream`, `stream_format`, `repetition_penalty`) and lets
`extra_body` carry anything else, so a future pod field needs no gateway change.

`Drama3VoiceConfig` implements the multipart upload, list, and delete against the
pod's own registry rather than the generic JSON `acreate_voice` action.

Two things to settle before building it:

- The controller would need to register the model with the new provider instead of
  `hosted_vllm`. That is a `provider` change in the discovery controller, gated the
  same way the tier-0 task change was.
- Streaming needs the gateway route to stop buffering. `voice_routes.py`'s
  `audio_speech` already builds a `StreamingResponse`, so the buffering is likely
  in the OpenAI handler returning a complete `HttpxBinaryResponseContent`. Worth
  confirming before promising streaming in the same change.

### Cheaper alternative, if the custom provider is too much

Register `hosted_vllm` in `get_provider_text_to_speech_config` with a config that
forwards the body verbatim. That fixes defect 1 for Drama 3 and for every other
self-hosted TTS server without a new provider enum, but leaves both voice-routers
broken. It is a much smaller diff and covers the capability gap that actually
matters to callers today (cloning and sampling), if not voice management.

## Current workaround

Call the pod directly. It needs no auth:

```bash
kubectl -n adeo port-forward deploy/j-51096511-94c6-4240-901a-86cccb5eb08a 8080:8080
```

## Piece not verified

`StreamingResponse` exists at the route level, so the buffering may be removable
without touching the OpenAI handler. Not investigated. Do not promise streaming
until it is measured end to end.
