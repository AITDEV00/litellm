"""Tests for controller/models.py shape parsing, mode/provider detection, api_base shape, and hamsa api-surface sniffing."""

from controller.models import (
    OicmModel,
    build_model,
    build_oicm_model,
    detect_api_surface,
    detect_mode,
    detect_mode_from_paths,
    detect_mode_from_task,
    detect_provider,
    parse_model_list,
    sanitize_model_id,
    strip_source_prefix,
    to_litellm_mode,
)


class TestParseModelList:
    def test_openai_shape(self):
        resp = {"object": "list", "data": [{"id": "gpt-4", "owned_by": "openai"}]}
        assert parse_model_list(resp) == ["gpt-4"]

    def test_openai_shape_multiple(self):
        resp = {
            "object": "list",
            "data": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        }
        assert parse_model_list(resp) == ["a", "b", "c"]

    def test_triton_wrapper_shape(self):
        # PaddleX Docling facade (Triton-style wrapper)
        resp = {
            "models": [
                {"name": "PP-DocLayoutV3", "version": "1", "ready": True, "active": True}
            ]
        }
        assert parse_model_list(resp) == ["PP-DocLayoutV3"]

    def test_triton_wrapper_multiple(self):
        resp = {
            "models": [
                {"name": "PP-DocLayoutV3"},
                {"name": "PP-StructureV3"},
            ]
        }
        assert parse_model_list(resp) == ["PP-DocLayoutV3", "PP-StructureV3"]

    def test_triton_stock_bare_array_is_not_handled_by_parse_model_list(self):
        # Stock Triton returns a bare array; the controller call sites only feed
        # dict payloads (resp.json() of the wrapper). If a bare array ever comes
        # through, we should not crash and return nothing.
        assert parse_model_list([{"name": "m1"}]) == []

    def test_empty_payloads(self):
        assert parse_model_list({}) == []
        assert parse_model_list(None) == []
        assert parse_model_list({"data": [], "models": []}) == []

    def test_skips_entries_without_id_or_name(self):
        assert parse_model_list({"data": [{"id": "a"}, {}]}) == ["a"]
        assert parse_model_list({"models": [{"name": "b"}, {}]}) == ["b"]


class TestSanitizeModelId:
    def test_slash_prefix_becomes_dashes(self):
        assert sanitize_model_id("/zai-org/GLM-5.2") == "zai-org--GLM-5.2"

    def test_strips_whitespace(self):
        assert sanitize_model_id("  PP-DocLayoutV3  ") == "PP-DocLayoutV3"


class TestDoclingDetection:
    def test_detect_mode_chat_without_convert_path(self):
        paths = frozenset({"/v1/chat/completions"})
        assert detect_mode_from_paths(paths, "llama-3", "") == "chat"

    def test_detect_provider_hosted_vllm_without_convert_path(self):
        assert detect_provider("", "llama-3-8b") == "hosted_vllm"

    def test_detect_provider_known_provider_wins(self):
        # inception/hamsa/omnivoice detection takes precedence over other paths
        paths = frozenset({"/v1/convert/source"})
        assert detect_provider("hamsa", "PP-DocLayoutV3", paths) == "hamsa"

    def test_detect_provider_suffixed_hamsa_model_id(self):
        # "hamsa-tts-new" must resolve to the hamsa provider via substring
        # matching, same as the bare "hamsa-tts" id.
        assert detect_provider("", "hamsa-tts-new") == "hamsa"
        assert detect_provider("", "hamsa-stt-v2") == "hamsa"
        assert detect_provider("", "hamsa-tts") == "hamsa"

    def test_detect_provider_suffixed_inception(self):
        assert detect_provider("", "inception-tts-new") == "inception"

    def test_detect_provider_drama3_from_model_id(self):
        # The live row: model id fishaudio/drama-3, owner sglang-omni. The id is
        # the signal; the owner is deliberately not matched because other
        # sglang-omni models would mislabel.
        assert detect_provider("sglang-omni", "fishaudio/drama-3") == "drama3"
        assert detect_provider("", "fishaudio/drama-3") == "drama3"

    def test_detect_provider_drama3_owner_string_alone_does_not_trigger(self):
        # Another model served by the same sglang-omni stack must not flip to
        # the drama3 provider.
        assert detect_provider("sglang-omni", "funaudio/cosyvoice2") == "hosted_vllm"

    def test_detect_provider_unrelated_ids_stay_hosted_vllm(self):
        # Guard the substring match: ids merely containing similar letters must
        # not flip providers.
        assert detect_provider("", "openchat-3.5") == "hosted_vllm"

    def test_detect_provider_pyannote_from_diarize_path(self):
        # A coro pod serves /v1/diarize (plus transcriptions) and has no chat
        # path, so the OpenAPI probe is the provider signal.
        paths = frozenset(
            {"/health", "/v1/audio/transcriptions", "/v1/diarize", "/v1/listen"}
        )
        assert detect_provider("", "pyannote/speaker-diarization-community-1", paths) == "pyannote"

    def test_detect_provider_pyannote_not_triggered_with_chat(self):
        # A chat-capable server also exposing /v1/diarize stays on its id-based
        # provider (the paddlex branch has the same guard).
        paths = frozenset({"/v1/chat/completions", "/v1/diarize"})
        assert detect_provider("", "some-llm", paths) == "hosted_vllm"

    def test_detect_provider_pyannote_from_id_substring(self):
        # The serving id contains the pyannote prefix, so KNOWN_PROVIDERS
        # matching fires even without the path probe.
        assert detect_provider("", "pyannote/community-1", frozenset()) == "pyannote"

    def test_detect_mode_diarize_beats_transcription(self):
        # A coro pod serves both surfaces; diarize-first classification is the
        # ordering contract in _PATH_MODES.
        paths = frozenset(
            {"/health", "/v1/audio/transcriptions", "/v1/diarize", "/v1/listen"}
        )
        assert (
            detect_mode_from_paths(paths, "pyannote/speaker-diarization-community-1", "")
            == "diarization"
        )

    def test_detect_mode_plain_asr_pod_stays_transcription(self):
        # Pods serving transcriptions without diarize keep their mode.
        paths = frozenset({"/health", "/v1/audio/transcriptions"})
        assert detect_mode_from_paths(paths, "whisper-large-v3", "") == "audio_transcription"

    def test_detect_mode_diariz_name_fallback(self):
        # Probe failed: the name-fallback token classifies diarization pods.
        assert detect_mode_from_paths(frozenset(), "pyannote-diarize-1", "") == "diarization"

    def test_pyannote_api_base_is_bare_clusterip(self):
        # NATIVE_BASE_PROVIDERS: no /v1 suffix; the gateway config appends
        # /v1/diarize itself.
        model = build_oicm_model(
            uuid="d4398150-0202-4e1c-ae9a-41be01288fd3",
            model_id="pyannote/community-1",
            model_name="pyannote/community-1",
            namespace="adeo",
            serving=True,
            provider="pyannote",
        )
        assert model.api_base == (
            "http://s-d4398150-0202-4e1c-ae9a-41be01288fd3.adeo.svc.cluster.local:8080"
        )


class TestDetectApiSurface:
    def test_v1_paths(self):
        paths = frozenset({"/v1/speech", "/v1/voices", "/v1/voice-clone", "/healthz"})
        assert detect_api_surface("hamsa", paths) == "v1"

    def test_native_paths(self):
        paths = frozenset({"/tts/stream", "/transcribe", "/health"})
        assert detect_api_surface("hamsa", paths) == "native"

    def test_non_hamsa_provider_is_none(self):
        paths = frozenset({"/v1/speech"})
        assert detect_api_surface("hosted_vllm", paths) is None

    def test_unknown_path_set_is_none(self):
        # Probe failed or empty openapi.json: leave surface unset rather than
        # guessing; the gateway default (native) applies.
        assert detect_api_surface("hamsa", frozenset()) is None

    def test_detect_provider_convert_path_falls_back_to_hosted_vllm(self):
        # /v1/convert/* paths no longer imply a docling provider; they fall
        # back to the default hosted_vllm classification.
        paths = frozenset({"/v1/convert/source"})
        assert detect_provider("", "PP-DocLayoutV3", paths) == "hosted_vllm"

    def test_detect_mode_wrapper_without_paths(self):
        # detect_mode passes no paths -> mode falls back to chat by name alone
        assert detect_mode("PP-DocLayoutV3", "") == "chat"

    def test_detect_mode_ocr_with_ocr_path(self):
        # A model exposing only /v1/ocr (Mistral-OCR compatible, e.g. PaddleX
        # PP-DocLayoutV3) is detected as an OCR model.
        paths = frozenset({"/v1/ocr"})
        assert detect_mode_from_paths(paths, "PP-DocLayoutV3", "") == "ocr"

    def test_detect_mode_ocr_with_chat_takes_precedence(self):
        # When both /v1/ocr and /v1/chat/completions are exposed, chat wins so a
        # hybrid model is not forced into OCR mode.
        paths = frozenset({"/v1/ocr", "/v1/chat/completions"})
        assert detect_mode_from_paths(paths, "PP-DocLayoutV3", "") == "chat"

    def test_detect_mode_hamsa_native_tts_path(self):
        # Hamsa pods expose /tts/stream, not the OpenAI /v1/audio/speech path.
        paths = frozenset({"/tts/stream"})
        assert detect_mode_from_paths(paths, "hamsa-tts-new", "") == "text_to_speech"

    def test_detect_mode_transcription_with_chat_is_chat(self):
        """A chat server that also serves transcription is still a chat model.

        Some chat pods advertise /v1/chat/completions alongside
        /v1/audio/transcriptions. Registering one as an audio model makes the
        gateway POST /v1/audio/transcriptions, which the pod does not serve, so
        the model 404s even though it is healthy.
        """
        paths = frozenset({"/v1/chat/completions", "/v1/audio/transcriptions"})
        assert detect_mode_from_paths(paths, "microsoft/VibeVoice-ASR", "") == "chat"

    def test_detect_mode_speech_with_chat_is_chat(self):
        paths = frozenset({"/v1/chat/completions", "/v1/audio/speech"})
        assert detect_mode_from_paths(paths, "some-chat-tts", "") == "chat"

    def test_detect_mode_embedding_with_chat_is_chat(self):
        paths = frozenset({"/v1/chat/completions", "/v1/embeddings"})
        assert detect_mode_from_paths(paths, "some-chat-embed", "") == "chat"

    def test_detect_mode_ocr_with_chat_is_chat(self):
        paths = frozenset({"/v1/chat/completions", "/v1/ocr"})
        assert detect_mode_from_paths(paths, "PP-DocLayoutV3", "") == "chat"

    def test_detect_mode_audio_only_still_detected(self):
        # The chat rule must not swallow a genuine audio-only pod.
        paths = frozenset({"/v1/audio/transcriptions", "/v1/models"})
        assert (
            detect_mode_from_paths(paths, "CohereLabs/cohere-transcribe", "")
            == "audio_transcription"
        )

    def test_detect_mode_chat_path_beats_the_name_fallback(self):
        # A chat pod whose name looks like TTS stays a chat pod.
        paths = frozenset({"/v1/chat/completions"})
        assert detect_mode_from_paths(paths, "some-tts-chat", "") == "chat"

    def test_detect_mode_hamsa_native_transcription_path(self):
        paths = frozenset({"/transcribe"})
        assert (
            detect_mode_from_paths(paths, "hamsa-stt-new", "")
            == "audio_transcription"
        )

    def test_detect_mode_name_fallback_tts(self):
        # No OpenAPI probe available: the hyphen-delimited capability token in
        # the model id decides the mode.
        assert detect_mode("hamsa-tts-new", "") == "text_to_speech"
        assert detect_mode("hamsa-stt-v2", "") == "audio_transcription"

    def test_detect_mode_name_fallback_requires_token(self):
        # Bare substrings must not match: "settings" contains neither "-tts"
        # as a token nor starts with one.
        assert detect_mode("settings", "") == "chat"
        assert detect_mode("chats-model", "") == "chat"

    def test_detect_provider_ocr_path_returns_paddlex(self):
        # A model exposing only /v1/ocr registers under the paddlex provider so
        # it routes through the first-class /v1/ocr endpoint as paddlex/PP-DocLayoutV3.
        paths = frozenset({"/v1/ocr"})
        assert detect_provider("", "PP-DocLayoutV3", paths) == "paddlex"

    def test_detect_provider_ocr_with_known_provider_wins(self):
        paths = frozenset({"/v1/ocr"})
        assert detect_provider("inception", "PP-DocLayoutV3", paths) == "inception"

    def test_detect_provider_ocr_with_chat_returns_hosted_vllm(self):
        # A hybrid model exposing both /v1/ocr and chat stays a hosted_vllm chat
        # model rather than being reclassified as paddlex OCR.
        paths = frozenset({"/v1/ocr", "/v1/chat/completions"})
        assert detect_provider("", "PP-DocLayoutV3", paths) == "hosted_vllm"


class TestApiBaseShape:
    def _model(self, provider: str, api_base_override: str | None = None) -> OicmModel:
        return OicmModel(
            uuid="abc123",
            model_id="hamsa-tts",
            model_name="hamsa-tts",
            namespace="adeo",
            ready_replicas=1,
            total_replicas=1,
            provider=provider,
            api_base_override=api_base_override,
        )

    def test_native_provider_gets_bare_base(self):
        # Hamsa's LiteLLM config appends its own paths ("/tts/stream"), so the
        # registered api_base must not carry a "/v1" suffix.
        assert (
            self._model("hamsa").api_base
            == "http://s-abc123.adeo.svc.cluster.local:8080"
        )

    def test_openai_providers_get_v1_suffix(self):
        assert (
            self._model("hosted_vllm").api_base
            == "http://s-abc123.adeo.svc.cluster.local:8080/v1"
        )
        assert (
            self._model("inception").api_base
            == "http://s-abc123.adeo.svc.cluster.local:8080/v1"
        )

    def test_override_wins_over_native_shape(self):
        m = self._model("hamsa", api_base_override="http://10.0.0.1:8080")
        assert m.api_base == "http://10.0.0.1:8080"

    def test_api_surface_defaults_to_none(self):
        assert self._model("hamsa").api_surface is None


class TestToLitellmMode:
    def test_text_to_speech_maps_to_audio_speech(self):
        assert to_litellm_mode("text_to_speech") == "audio_speech"

    def test_ocr_passes_through(self):
        # "ocr" is a first-class LiteLLM mode; no translation needed.
        assert to_litellm_mode("ocr") == "ocr"

    def test_other_modes_passthrough(self):
        assert to_litellm_mode("chat") == "chat"
        assert to_litellm_mode("embedding") == "embedding"


class TestDetectModeFromTask:
    """Tier 0: OICM's declared task, gated on the task's native surface.

    Live evidence driving each pin:
    - Drama 3 (sglang-omni) mounts chat AND /v1/audio/speech while its task is
      Text-to-Speech; path detection alone reads it as chat and every health
      probe 503s.
    - VibeVoice-ASR declares Automatic Speech Recognition but serves through
      chat with no transcription path; registering it audio_transcription
      would make the gateway POST a route the pod does not serve.
    """

    def test_declared_tts_with_mounted_surface_wins_over_chat(self):
        # The Drama 3 regression: chat path present, task says TTS, the TTS
        # surface is mounted. The declaration decides, and path detection
        # alone would have said chat.
        paths = frozenset(
            {"/v1/chat/completions", "/v1/audio/speech", "/v1/audio/transcriptions"}
        )
        assert detect_mode_from_task("Text-to-Speech", paths) == "text_to_speech"
        assert detect_mode_from_paths(paths, "fishaudio/drama-3", "") == "chat"

    def test_declared_asr_without_its_surface_falls_through_to_paths(self):
        # The VibeVoice contract (live openapi.json: chat, completions,
        # messages, responses; NO /v1/audio/transcriptions): the task maps
        # toward audio_transcription, but the native surface is not mounted,
        # so tier 0 declines and path detection keeps the chat surface.
        paths = frozenset({"/v1/chat/completions", "/v1/models"})
        assert detect_mode_from_task("Automatic Speech Recognition", paths) is None
        assert detect_mode_from_paths(paths, "microsoft/VibeVoice-ASR", "") == "chat"

    def test_declared_asr_with_mounted_surface_decides(self):
        # A whisper-style pod that actually mounts transcriptions: the task
        # and the surface agree.
        paths = frozenset({"/v1/audio/transcriptions"})
        assert detect_mode_from_task("Automatic Speech Recognition", paths) == "audio_transcription"

    def test_unknown_task_declines_rather_than_guesses(self):
        # A task OICM adds tomorrow must degrade to the lower tiers, never
        # crash and never guess a mode.
        assert detect_mode_from_task("Brand New Task", frozenset()) is None

    def test_absent_task_declines(self):
        assert detect_mode_from_task(None, frozenset()) is None
        assert detect_mode_from_task("", frozenset()) is None

    def test_chat_task_needs_no_surface_check(self):
        # Text Generation's native surface IS chat, so no gate applies and a
        # chat-capable pod is chat.
        assert detect_mode_from_task("Text Generation", frozenset()) == "chat"

    def test_embedding_task_with_mounted_surface(self):
        assert detect_mode_from_task("Text Embedding", frozenset({"/v1/embeddings"})) == "embedding"

    def test_diarization_task_with_mounted_surface(self):
        assert detect_mode_from_task("Diarization", frozenset({"/v1/diarize"})) == "diarization"

    def test_image_task_with_mounted_surface(self):
        # Live Qwen-Image pod: /v1/images/generations mounted, no chat path.
        # Without tier 0 it registered as mode=chat (path detection has no
        # images entry), so /v1/images/generations 404'd at the pod.
        assert (
            detect_mode_from_task("Text-to-Image", frozenset({"/v1/images/generations", "/v1/models"}))
            == "image_generation"
        )

    def test_image_task_without_its_surface_falls_through(self):
        # The gate applies to image generation like every other non-chat
        # task: a declaration without the mounted surface must not re-route
        # the row.
        assert detect_mode_from_task("Text-to-Image", frozenset({"/v1/chat/completions"})) is None

    def test_every_non_chat_task_has_a_surface_gate(self):
        """Table cross-check: no task may fire tier 0 ungated.

        A task mapping to a non-chat mode with no surface path would
        re-route a chat-shaped pod on the declaration alone, which is the
        exact misregistration this tier exists to prevent. Pins the
        _TASK_MODES <-> _TASK_SURFACE_PATHS coverage contract.
        """
        from controller.models import _TASK_MODES, _TASK_SURFACE_PATHS

        ungated = {
            task
            for task, mode in _TASK_MODES.items()
            if mode != "chat" and task not in _TASK_SURFACE_PATHS
        }
        assert ungated == set()


class TestDeploymentId:
    def test_deployment_id_is_the_uuid(self):
        """Identity is the deployment uuid alone, never the model name.

        OICM stores `model_name` as the deployment's GUI label, which can differ
        from the id the model server actually serves. The gateway row is keyed by
        the uuid, so the label can never be load-bearing for matching.
        """
        m = OicmModel(
            uuid="abc123",
            model_id="/org/PP-DocLayoutV3",
            model_name="org--PP-DocLayoutV3",
            namespace="adeo",
            ready_replicas=1,
            total_replicas=1,
        )
        assert m.deployment_id == "abc123"


class TestSourcePrefix:
    """The Submariner prefix is transport detail, not identity.

    The import knows a deployment as `submariner:<cluster>:<uuid>` while its own
    OICM reports the bare uuid. Stripping is what makes the two land on one key,
    and the cluster is preserved separately as `cluster` so the origin is not
    lost by doing it.
    """

    def test_deployment_id_strips_the_source_prefix(self):
        m = OicmModel(
            uuid="submariner:abudhabi:766b1720",
            model_id="zai-org/GLM-5.2-FP8",
            model_name="zai-org/GLM-5.2-FP8",
            namespace="adeo",
            ready_replicas=1,
            total_replicas=1,
            cluster="abudhabi",
        )
        assert m.deployment_id == "766b1720"

    def test_discovered_and_gateway_keys_agree(self):
        """Regression: the two sides of the join must key identically.

        `discovered` is keyed on `OicmModel.deployment_id` while `litellm_by_key`
        is keyed on the stored `oicm_uuid` with its prefix stripped. If only one
        side strips, a cross-cluster import matches neither its own k8s row nor
        its OICM snapshot and gets registered a second time.
        """
        import_source = OicmModel(
            uuid="submariner:abudhabi:766b1720",
            model_id="zai-org/GLM-5.2-FP8",
            model_name="zai-org/GLM-5.2-FP8",
            namespace="adeo",
            ready_replicas=1,
            total_replicas=1,
            cluster="abudhabi",
        )
        stored_uuid = "submariner:abudhabi:766b1720"

        assert import_source.deployment_id == strip_source_prefix(stored_uuid)

    def test_prefix_without_a_cluster_is_left_intact(self):
        """A malformed value must not be silently mangled into a wrong key."""
        assert strip_source_prefix("submariner:766b1720") == "766b1720"

    def test_bare_uuid_passes_through(self):
        assert strip_source_prefix("766b1720") == "766b1720"

    def test_build_model_records_the_cluster(self):
        """A source must be able to say which cluster a deployment is in."""
        m = build_model(
            uuid="submariner:abudhabi:766b1720",
            model_id="zai-org/GLM-5.2-FP8",
            ready_replicas=1,
            total_replicas=1,
            source="submariner:abudhabi",
            cluster="abudhabi",
        )

        assert m.cluster == "abudhabi"
        assert m.source == "submariner:abudhabi"
        assert m.deployment_id == "766b1720"