"""Tests for the exclusions slice: which identities are excluded, and matching.

Pins behavior, not structure: a malformed document must never raise (exclusion
is a safety net, so the worst case of missing it is a model that stays
registered, whereas raising could stall the controller), the env override merges
with the file, and an entry matches any identity an operator is likely to write.
"""

import textwrap
from pathlib import Path

from controller.exclusions import excluded, load_exclusions, parse_exclusions
from controller.models import OicmModel


def _model(**overrides) -> OicmModel:
    base = {
        "uuid": "766b1720-f516-4077-b22c-6ce97c045470",
        "model_id": "Qwen/Qwen3.6-35B-A3B-FP8",
        "model_name": "Qwen--Qwen3.6-35B-A3B-FP8",
        "namespace": "adeo",
        "ready_replicas": 1,
        "total_replicas": 1,
    }
    return OicmModel(**{**base, **overrides})


def _write(tmp_path, text):
    path = tmp_path / "exclusions.yaml"
    path.write_text(text)
    return str(path)


class TestParse:
    def test_bare_list(self):
        assert parse_exclusions(["a", "b"]) == frozenset({"a", "b"})

    def test_mapping_with_model_ids(self):
        assert parse_exclusions({"model_ids": ["a"]}) == frozenset({"a"})

    def test_blank_entries_are_dropped(self):
        assert parse_exclusions(["a", "  ", ""]) == frozenset({"a"})

    def test_non_string_entries_are_dropped(self):
        assert parse_exclusions(["a", 1, None]) == frozenset({"a"})

    def test_malformed_document_yields_empty_not_raise(self):
        # Exclusion must never stop the controller; a bad file degrades to
        # "nothing excluded", which is recoverable.
        assert parse_exclusions("not a list or mapping") == frozenset()
        assert parse_exclusions(None) == frozenset()
        assert parse_exclusions({"wrong_key": ["a"]}) == frozenset()


class TestLoad:
    def test_loads_from_file(self, tmp_path):
        path = _write(tmp_path, "model_ids:\n  - Excluded-Model\n")
        assert load_exclusions(path, env={}) == frozenset({"Excluded-Model"})

    def test_missing_file_returns_empty_not_raise(self, tmp_path):
        assert load_exclusions(str(tmp_path / "nope.yaml"), env={}) == frozenset()

    def test_env_ids_merge_with_the_file(self, tmp_path):
        path = _write(tmp_path, "model_ids:\n  - From-File\n")
        ids = load_exclusions(path, env={"OICM_EXCLUDED_MODEL_IDS": "From-Env,Another"})
        assert ids == frozenset({"From-File", "From-Env", "Another"})

    def test_env_ids_apply_without_a_file(self, tmp_path):
        ids = load_exclusions(
            str(tmp_path / "nope.yaml"), env={"OICM_EXCLUDED_MODEL_IDS": "Only-Env"}
        )
        assert ids == frozenset({"Only-Env"})

    def test_configmap_wrapper_is_unwrapped(self, tmp_path):
        """The committed file is applied as a ConfigMap and read locally.

        One file serves both roles, so a local run must unwrap the ConfigMap
        rather than needing a second copy that can drift.
        """
        wrapped = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: oicm-exclusions\n"
            "data:\n  exclusions.yaml: |\n"
            + textwrap.indent("model_ids:\n  - Excluded-Model\n", "    ")
        )
        path = _write(tmp_path, wrapped)
        assert load_exclusions(path, env={}) == frozenset({"Excluded-Model"})

    def test_repository_exclusions_files_are_valid(self):
        """Both committed ConfigMaps must parse. Prod ships empty by default."""
        from controller.exclusions import _LOCAL_EXCLUSIONS_FILE

        prod = Path(__file__).resolve().parents[2] / "deploy" / "oicm" / "exclusions.yaml"
        dev = Path(__file__).resolve().parents[2] / "deploy" / "dev" / "oicm-exclusions-dev.yaml"

        assert load_exclusions(str(prod), env={}) == frozenset()
        assert load_exclusions(str(dev), env={}) == frozenset(
            {"orcarouter/Qwen3.8-27B-Uncensored-FP8"}
        )
        # The repo-local fallback must point at the prod list, so a local run
        # matches what prod serves rather than picking up dev's exclusions.
        assert _LOCAL_EXCLUSIONS_FILE == prod


class TestExcluded:
    def test_no_ids_excludes_nothing(self):
        assert excluded(_model(), frozenset()) is False

    def test_matches_the_served_model_id(self):
        assert excluded(_model(), frozenset({"Qwen/Qwen3.6-35B-A3B-FP8"})) is True

    def test_matches_the_sanitized_gateway_name(self):
        assert excluded(_model(), frozenset({"Qwen--Qwen3.6-35B-A3B-FP8"})) is True

    def test_matches_the_deployment_uuid(self):
        assert excluded(_model(), frozenset({"766b1720-f516-4077-b22c-6ce97c045470"})) is True

    def test_matches_a_submariner_import_by_bare_uuid(self):
        """A Submariner import's uuid is prefixed, so the bare id must match too."""
        model = _model(
            uuid="submariner:abudhabi:766b1720-f516-4077-b22c-6ce97c045470",
            model_id="zai-org/GLM-5.2-FP8",
        )
        assert excluded(model, frozenset({"766b1720-f516-4077-b22c-6ce97c045470"})) is True

    def test_a_different_model_is_not_excluded(self):
        assert excluded(_model(), frozenset({"Some-Other-Model"})) is False
