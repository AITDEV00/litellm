"""Tests for the install-script generator."""

from __future__ import annotations

import pytest

from jinja2 import UndefinedError

from litellm.proxy.client_distribution.generator import (
    MirrorManifest,
    PlatformTarget,
    manifest_from_json,
    render_install_script,
)

_PLATFORMS: tuple[PlatformTarget, ...] = (
    PlatformTarget(target="x86_64-unknown-linux-gnu", archive_ext="tar.gz"),
    PlatformTarget(target="aarch64-apple-darwin", archive_ext="tar.gz"),
    PlatformTarget(target="x86_64-pc-windows-msvc", archive_ext="zip"),
)
_MANIFEST: MirrorManifest = MirrorManifest(
    uv_version="0.10.9",
    litellm_cli_version="1.102.0",
    python_version="3.13.9",
    platforms=_PLATFORMS,
)
_BASE_URL: str = "https://litellm-dev.example.com"


def test_render_substitutes_base_url() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert _BASE_URL in script
    assert "{{ base_url }}" not in script


def test_render_substitutes_uv_version() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert 'UV_VERSION="0.10.9"' in script
    assert "{{ uv_version }}" not in script


def test_render_substitutes_cli_version() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert 'LITELLM_CLI_VERSION="1.102.0"' in script
    assert "{{ litellm_cli_version }}" not in script


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


def test_render_has_no_state_machine() -> None:
    """The simplified resume design drops the install_state.json machine."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "install_state.json" not in script
    assert "NEED_UV" not in script
    assert "NEED_LITE" not in script


def test_render_has_no_harness_detection() -> None:
    """Harness detection is report-only UX owned by the CLI, not the script."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "Detected harnesses:" not in script


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
    raw: str = (
        '{"uv_version": "0.10.9", "litellm_cli_version": "1.102.0", '
        '"python_version": "3.13.9", '
        '"platforms": [{"target": "x86_64-unknown-linux-gnu", "archive_ext": "tar.gz"}]}'
    )
    m: MirrorManifest = manifest_from_json(raw)
    assert m.uv_version == "0.10.9"
    assert m.litellm_cli_version == "1.102.0"
    assert m.python_version == "3.13.9"
    assert m.platforms == (
        PlatformTarget(target="x86_64-unknown-linux-gnu", archive_ext="tar.gz"),
    )


def test_manifest_from_json_platforms_optional() -> None:
    raw: str = '{"uv_version": "0.10.9", "litellm_cli_version": "1.102.0", "python_version": "3.13.9"}'
    m: MirrorManifest = manifest_from_json(raw)
    assert m.platforms == ()


def test_manifest_from_json_platform_entry_missing_ext() -> None:
    raw: str = (
        '{"uv_version": "0.10.9", "litellm_cli_version": "1.102.0", '
        '"python_version": "3.13.9", '
        '"platforms": [{"target": "x86_64-unknown-linux-gnu"}]}'
    )
    with pytest.raises(ValueError, match="archive_ext"):
        manifest_from_json(raw)


def test_manifest_from_json_missing_uv_version() -> None:
    with pytest.raises(ValueError, match="uv_version"):
        manifest_from_json('{"litellm_cli_version": "1.102.0", "python_version": "3.13.9"}')


def test_manifest_from_json_missing_cli_version() -> None:
    with pytest.raises(ValueError, match="litellm_cli_version"):
        manifest_from_json('{"uv_version": "0.10.9", "python_version": "3.13.9"}')


def test_manifest_from_json_invalid_json() -> None:
    with pytest.raises(ValueError, match="invalid manifest JSON"):
        manifest_from_json("not json")


# ── Jinja2 rendering contract ───────────────────────────────────────────────


def test_render_undefined_variable_raises() -> None:
    """StrictUndefined: a template referencing an unknown variable must fail."""
    with pytest.raises(UndefinedError):
        render_install_script(_MANIFEST, _BASE_URL, template="gateway={{ no_such_var }}")


def test_render_does_not_html_escape_shell() -> None:
    """autoescape=False: shell metacharacters must not become HTML entities."""
    script: str = render_install_script(
        _MANIFEST, _BASE_URL, template="run if a > b && c < d; {{ base_url }}"
    )
    assert "a > b && c < d" in script
    assert "&gt;" not in script
    assert "&amp;" not in script


def test_render_loop_over_platforms() -> None:
    """Structured context: {% for %} over platforms renders each target."""
    template: str = "{% for p in platforms %}{{ p.target }}={{ p.archive_ext }} {% endfor %}"
    script: str = render_install_script(_MANIFEST, _BASE_URL, template=template)
    assert "x86_64-unknown-linux-gnu=tar.gz" in script
    assert "x86_64-pc-windows-msvc=zip" in script


def test_render_store_and_baked_identical() -> None:
    """A store template equal to the baked-in one renders identically."""
    from litellm.proxy.client_distribution.generator import get_template

    a: str = render_install_script(_MANIFEST, _BASE_URL)
    b: str = render_install_script(_MANIFEST, _BASE_URL, template=get_template())
    assert a == b
