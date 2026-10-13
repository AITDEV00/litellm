"""Tests for the install-script generator."""

from __future__ import annotations

import pytest

from litellm.proxy.client_distribution.generator import (
    MirrorManifest,
    manifest_from_json,
    render_install_script,
)

_MANIFEST: MirrorManifest = MirrorManifest(
    uv_version="0.10.9",
    litellm_cli_version="1.102.0",
    python_version="3.13.9",
)
_BASE_URL: str = "https://litellm-dev.example.com"


def test_render_substitutes_base_url() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert _BASE_URL in script
    assert "{{BASE_URL}}" not in script


def test_render_substitutes_uv_version() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert 'UV_VERSION="0.10.9"' in script
    assert "{{UV_VERSION}}" not in script


def test_render_substitutes_cli_version() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert 'LITELLM_CLI_VERSION="1.102.0"' in script
    assert "{{LITELLM_CLI_VERSION}}" not in script


def test_render_has_gateway_config_set() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert f'config set base_url "$BASE_URL"' in script or f'config set base_url "{_BASE_URL}"' in script


def test_render_uses_no_index_find_links() -> None:
    """The uv tool install must use --no-index --find-links to prevent PyPI escape."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "--no-index" in script
    assert "--find-links" in script


def test_render_uses_mirror_for_uv_bootstrap() -> None:
    """The uv installer must come from the gateway, not astral.sh."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "astral.sh" not in script
    assert "$BASE_URL/install/uv/" in script or f"{_BASE_URL}/install/uv/" in script


def test_render_uses_mirror_for_python() -> None:
    """UV_PYTHON_INSTALL_MIRROR must point at the gateway."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "UV_PYTHON_INSTALL_MIRROR" in script
    assert "$BASE_URL/install/python" in script or f"{_BASE_URL}/install/python" in script


def test_render_no_upstream_urls() -> None:
    """No upstream URL should appear in the rendered script."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    for upstream in ("astral.sh", "github.com", "pypi.org"):
        assert upstream not in script, f"upstream URL leaked: {upstream}"


def test_render_has_phase_markers() -> None:
    """The resume design requires phase A markers in the script."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "install_state.json" in script
    assert "NEED_UV" in script
    assert "NEED_LITE" in script


def test_render_has_harness_detection() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "Detected harnesses:" in script
    assert "claude" in script
    assert "codex" in script


def test_render_deterministic() -> None:
    a: str = render_install_script(_MANIFEST, _BASE_URL)
    b: str = render_install_script(_MANIFEST, _BASE_URL)
    assert a == b


def test_render_different_base_url() -> None:
    a: str = render_install_script(_MANIFEST, "https://a.example.com")
    b: str = render_install_script(_MANIFEST, "https://b.example.com")
    assert a != b
    assert "https://a.example.com" in a
    assert "https://b.example.com" in b


# ── manifest_from_json ──────────────────────────────────────────────────────


def test_manifest_from_json_valid() -> None:
    raw: str = '{"uv_version": "0.10.9", "litellm_cli_version": "1.102.0", "python_version": "3.13.9"}'
    m: MirrorManifest = manifest_from_json(raw)
    assert m.uv_version == "0.10.9"
    assert m.litellm_cli_version == "1.102.0"
    assert m.python_version == "3.13.9"


def test_manifest_from_json_missing_uv_version() -> None:
    with pytest.raises(ValueError, match="uv_version"):
        manifest_from_json('{"litellm_cli_version": "1.102.0", "python_version": "3.13.9"}')


def test_manifest_from_json_missing_cli_version() -> None:
    with pytest.raises(ValueError, match="litellm_cli_version"):
        manifest_from_json('{"uv_version": "0.10.9", "python_version": "3.13.9"}')


def test_manifest_from_json_invalid_json() -> None:
    with pytest.raises(ValueError, match="invalid manifest JSON"):
        manifest_from_json("not json")
