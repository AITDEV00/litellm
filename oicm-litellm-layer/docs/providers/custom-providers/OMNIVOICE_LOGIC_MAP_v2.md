# OmniVoice Logic Map v2

## Source: omnivoice-server (jya0-v0.2.4 branch)

### Endpoint Inventory

| # | Method | Path | Body | Returns |
|---|--------|------|------|---------|
| 1 | GET | /v1/voices | none | JSON {voices, design_attributes, total} |
| 2 | GET | /v1/models | none | JSON {object:"list", data:[3 models]} |
| 3 | GET | /v1/models/{model_id} | none | JSON single model |
| 4 | GET | /health | none | JSON {status, ready, model_loaded, ...} |
| 5 | GET | /metrics | none | JSON {requests_total, ...} |
| 6 | POST | /v1/audio/speech | JSON SpeechRequest | binary audio + headers |
| 7 | POST | /v1/audio/script | JSON ScriptRequest | binary (single_track) or JSON (multi_track) |
| 8 | POST | /v1/voices/profiles | multipart (profile_id, ref_audio, ref_text, overwrite) | JSON {profile_id, name, ...} 201 |
| 9 | GET | /v1/voices/profiles/{id} | none | JSON profile |
| 10 | PATCH | /v1/voices/profiles/{id} | multipart (ref_audio, ref_text) | JSON profile |
| 11 | DELETE | /v1/voices/profiles/{id} | none | 204 No Content |
| 12 | POST | /v1/audio/speech/clone | multipart (text, ref_audio, ref_text, ...) | binary audio + headers |

### Key Data Shapes

**SpeechRequest** (POST /v1/audio/speech): JSON with model, input, voice (default "auto"), instructions, response_format, speed, stream, num_step, guidance_scale, denoise, t_shift, position_temperature, class_temperature, duration, language, layer_penalty_factor, preprocess_prompt, postprocess_output, audio_chunk_duration, audio_chunk_threshold, request_timeout_s

**ScriptRequest** (POST /v1/audio/script): JSON with script (list of {speaker, text, voice, speed}), default_voice, speed, response_format, output_format ("single_track"|"multi_track"), pause_between_speakers, on_error ("abort"|"skip")

**Profile create** (POST /v1/voices/profiles): multipart with profile_id (required), ref_audio (file, required), ref_text (optional), overwrite (optional bool)

**Profile update** (PATCH /v1/voices/profiles/{id}): multipart with ref_audio (optional file), ref_text (optional)

### Auth Middleware
Skips auth for: /health, /health-check, /metrics, /v1/models. All other endpoints require Bearer token.

## LiteLLM Gateway: 5 Root Causes and Fixes

### Fix 1: GET endpoints fail (model=None)
**Trace**: proxy GET /v1/voices -> _route_voice_management -> data={} (no body) -> add_litellm_data_to_request -> model = data.pop("model", None) or user_model -> model=None -> route_request -> llm_router.alist_voices(model=None, ...) -> async_get_available_deployment(model=None) -> 500

**Fix**: In _route_voice_management, when model is None, find first audio_speech deployment from llm_router.model_list and use its model_name.

### Fix 2: POST /v1/audio/speech fails (missing voice arg)
**Trace**: proxy POST /v1/audio/speech -> data = JSON body -> route_request(route_type="aspeech") -> llm_router.aspeech(model=..., input=..., **data) -> if voice not in data -> TypeError: missing required positional argument 'voice'

OmniVoice server defaults voice to "auto". LiteLLM router requires voice as positional arg.

**Fix**: Make voice optional (default None) in Router.aspeech/_aspeech. Change OmniVoiceTextToSpeechConfig default from "alloy" to "auto".

### Fix 3: POST /v1/audio/script fails (wrong body shape)
**Trace**: proxy POST /v1/audio/script -> data.pop("input", None) -> None -> raises "input is required for script synthesis"

OmniVoice script endpoint uses `script` array, not `input` string.

**Fix**: Remove input requirement. Pass through script array and other script-specific fields. Make input/voice optional in Router.ascript/_ascript.

### Fix 4: Multipart endpoints fail (model=None + form data issues)
**Trace**: Same model=None issue as Fix 1. Additionally, OmniVoiceVoiceConfig.transform_create_voice_request uses _collect_passthrough(voice_data) which includes internal fields like "action" and the ref_audio tuple in form_fields.

**Fix**: Fix 1 resolves model=None. Fix transform_create_voice_request to only pick known form fields, not _collect_passthrough on voice_data. Fix update_profile to handle ref_audio files.

### Fix 5: GET /v1/models, /health, /metrics not proxied
**Trace**: LiteLLM serves its own /v1/models, /health, /metrics endpoints. OmniVoice pod's responses are never reached.

**Fix**: Add new proxy endpoints: GET /v1/audio/models, /v1/audio/models/{model_id}, /v1/audio/health, /v1/audio/metrics that forward to the pod.
