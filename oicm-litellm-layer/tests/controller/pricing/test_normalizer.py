from controller.pricing.normalizer import (
    normalize_model_name,
    parse_family_and_params,
    tokenize,
)


class TestNormalizeModelName:
    def test_strips_huggingface_org_prefix(self):
        assert normalize_model_name("Qwen/Qwen2.5-32B-Instruct") == "qwen2.5-32b"

    def test_strips_provider_and_org_prefix(self):
        assert (
            normalize_model_name("together_ai/Qwen/Qwen2.5-72B-Instruct-Turbo")
            == "qwen2.5-72b"
        )

    def test_strips_double_prefix(self):
        assert (
            normalize_model_name("openrouter/qwen/qwen3-coder") == "qwen3-coder"
        )

    def test_strips_bedrock_dotted_prefix_and_version(self):
        assert normalize_model_name("qwen.qwen3-32b-v1:0") == "qwen3-32b"

    def test_strips_meta_llama_prefix(self):
        assert normalize_model_name("meta-llama/Llama-3.1-8B-Instruct") == "llama-3.1-8b"

    def test_strips_deepseek_org_prefix(self):
        assert normalize_model_name("deepseek-ai/DeepSeek-V3") == "deepseek-v3"

    def test_bare_key_unchanged(self):
        assert normalize_model_name("deepseek-chat") == "deepseek-chat"

    def test_strips_fp8_suffix(self):
        assert (
            normalize_model_name("together_ai/Qwen/Qwen3-Coder-480B-A35B-Instruct-FP8")
            == "qwen3-coder-480b-a35b"
        )

    def test_strips_date_suffix(self):
        assert normalize_model_name("qwen3-coder-2025-01-25") == "qwen3-coder"

    def test_strips_long_date_suffix(self):
        assert normalize_model_name("claude-opus-4-20250514") == "claude-opus-4"

    def test_strips_stacking_suffixes(self):
        assert normalize_model_name("qwen2.5-32b-instruct-v1") == "qwen2.5-32b"

    def test_empty_string(self):
        assert normalize_model_name("") == ""

    def test_already_normalized(self):
        assert normalize_model_name("qwen-max") == "qwen-max"

    def test_underscores_to_dashes(self):
        assert normalize_model_name("some_model_name") == "some-model-name"

    def test_collapses_repeated_dashes(self):
        assert normalize_model_name("model--name") == "model-name"

    def test_strips_leading_trailing_dashes(self):
        assert normalize_model_name("-model-name-") == "model-name"

    def test_dotted_without_org_prefix_stays(self):
        assert normalize_model_name("deepseek.v3") == "deepseek.v3"

    def test_embedding_key_normalized(self):
        assert normalize_model_name("amazon.titan-embed-text-v1") == "titan-embed-text"


class TestParseFamilyAndParams:
    def test_extracts_family_and_param(self):
        assert parse_family_and_params("qwen2.5-32b") == ("qwen2.5", "32b")

    def test_extracts_family_with_version(self):
        assert parse_family_and_params("llama-3.1-8b") == ("llama-3.1", "8b")

    def test_no_param_count(self):
        assert parse_family_and_params("deepseek-v3") == ("deepseek-v3", None)

    def test_no_param_count_named(self):
        assert parse_family_and_params("qwen-max") == ("qwen-max", None)

    def test_coder_variant_no_param(self):
        assert parse_family_and_params("qwen3-coder") == ("qwen3-coder", None)

    def test_large_param(self):
        assert parse_family_and_params("qwen3-coder-480b-a35b") == (
            "qwen3-coder-480b-a35b",
            None,
        )

    def test_empty(self):
        assert parse_family_and_params("") == ("", None)


class TestTokenize:
    def test_basic(self):
        assert tokenize("qwen2.5-32b") == ("qwen2.5", "32b")

    def test_empty(self):
        assert tokenize("") == ()

    def test_single_token(self):
        assert tokenize("deepseek-chat") == ("deepseek", "chat")

    def test_filters_empty_tokens(self):
        assert tokenize("qwen--max") == ("qwen", "max")
