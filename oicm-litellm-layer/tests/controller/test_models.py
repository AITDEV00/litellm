"""Tests for controller/models.py shape parsing and docling detection."""

from controller.models import (
    OicmModel,
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

    def test_detect_provider_convert_path_falls_back_to_hosted_vllm(self):
        # /v1/convert/* paths no longer imply a docling provider; they fall
        # back to the default hosted_vllm classification.
        paths = frozenset({"/v1/convert/source"})
        assert detect_provider("", "PP-DocLayoutV3", paths) == "hosted_vllm"

    def test_detect_mode_wrapper_without_paths(self):
        # detect_mode passes no paths -> mode falls back to chat by name alone
        assert detect_mode("PP-DocLayoutV3", "") == "chat"


class TestToLitellmMode:
    def test_text_to_speech_maps_to_audio_speech(self):
        assert to_litellm_mode("text_to_speech") == "audio_speech"

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