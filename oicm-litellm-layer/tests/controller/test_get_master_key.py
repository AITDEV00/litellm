"""Tests for the get_master_key helper.

This is the single-source-of-truth extractor used by the Makefile, local
configs, and benchmarks. It must return exactly the value in
deploy/litellm-proxy.yaml and fail loudly (not silently) if that value is
missing, so a rotation can never be half-propagated.
"""

import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parents[2] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import get_master_key  # noqa: E402


@pytest.fixture
def manifest(tmp_path):
    return tmp_path / "litellm-proxy.yaml"


def test_reads_value_from_named_secret(manifest):
    manifest.write_text(
        "\n".join(
            [
                "---",
                "apiVersion: v1",
                "kind: Secret",
                "metadata:",
                "  name: litellm-master-key",
                "  namespace: mlops",
                "type: Opaque",
                "stringData:",
                "  master-key: sk-test-rotate",
                "---",
                "apiVersion: v1",
                "kind: Secret",
                "metadata:",
                "  name: other-secret",
                "stringData:",
                "  master-key: sk-wrong",
            ]
        )
    )
    assert get_master_key.master_key_from_manifest(manifest) == "sk-test-rotate"


def test_ignores_other_secrets_master_key(manifest):
    manifest.write_text(
        "\n".join(
            [
                "---",
                "kind: Secret",
                "metadata:",
                "  name: some-other-secret",
                "stringData:",
                "  master-key: sk-wrong",
            ]
        )
    )
    with pytest.raises(RuntimeError):
        get_master_key.master_key_from_manifest(manifest)


def test_missing_secret_raises(manifest):
    manifest.write_text("kind: ConfigMap\nmetadata:\n  name: unrelated\n")
    with pytest.raises(RuntimeError):
        get_master_key.master_key_from_manifest(manifest)


def test_missing_value_in_secret_raises(manifest):
    manifest.write_text(
        "\n".join(
            [
                "---",
                "kind: Secret",
                "metadata:",
                "  name: litellm-master-key",
                "stringData:",
                "  other-key: x",
            ]
        )
    )
    with pytest.raises(RuntimeError):
        get_master_key.master_key_from_manifest(manifest)