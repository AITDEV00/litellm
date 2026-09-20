"""Tests for controller/models.py shape parsing, mode/provider detection, api_base shape, and hamsa api-surface sniffing."""

from controller.models import (
    OicmModel,
    detect_api_surface,
    detect_mode,
    detect_mode_from_paths,
    detect_provider,
    parse_model_list,
    sanitize_model_id,
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

    def test_detect_provider_unrelated_ids_stay_hosted_vllm(self):
        # Guard the substring match: ids merely containing similar letters must
        # not flip providers.
        assert detect_provider("", "openchat-3.5") == "hosted_vllm"


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
    def _model(self, provider: str) -> OicmModel:
        return OicmModel(
            uuid="abc123",
            model_id="hamsa-tts",
            model_name="hamsa-tts",
            namespace="adeo",
            ready_replicas=1,
            total_replicas=1,
            provider=provider,
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
        m = self._model("hamsa")
        m.api_base_override = "http://10.0.0.1:8080"
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


class TestCompositeKey:
    def test_composite_key_uses_model_name(self):
        m = OicmModel(
            uuid="abc123",
            model_id="/org/PP-DocLayoutV3",
            model_name="org--PP-DocLayoutV3",
            namespace="adeo",
            ready_replicas=1,
            total_replicas=1,
        )
        assert m.composite_key == "abc123::org--PP-DocLayoutV3"