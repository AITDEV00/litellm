"""Tests for the install-script generator (cc-switch design)."""

from __future__ import annotations

import pytest

from jinja2 import UndefinedError

from litellm.proxy.client_distribution.generator import (
    HarnessPreset,
    MirrorManifest,
    PlatformTarget,
    get_baked_preset,
    manifest_from_json,
    render_install_script,
)

_PLATFORMS: tuple[PlatformTarget, ...] = (
    PlatformTarget(target="linux-x64-musl", archive_ext="tar.gz"),
    PlatformTarget(target="darwin-universal", archive_ext="tar.gz"),
    PlatformTarget(target="windows-x64", archive_ext="zip"),
)
_ARTIFACTS: dict[str, dict[str, str]] = {
    "linux-x64-musl": {"file": "cc-switch-cli-linux-x64-musl.tar.gz", "sha256": "a" * 64},
    "darwin-universal": {"file": "cc-switch-cli-darwin-universal.tar.gz", "sha256": "b" * 64},
    "windows-x64": {"file": "cc-switch-cli-windows-x64.zip", "sha256": "c" * 64},
}
_HARNESSES: tuple[HarnessPreset, ...] = (
    HarnessPreset(
        harness="open-code",
        version=3,
        path="providers/oicm/open-code/v3.json",
        sha256="d" * 64,
        mode="additive",
    ),
    HarnessPreset(
        harness="claude",
        version=1,
        path="",
        sha256="",
        mode="switch",
    ),
)
_MANIFEST: MirrorManifest = MirrorManifest(
    cc_switch_version="5.11.1",
    cc_switch_artifacts=_ARTIFACTS,
    harnesses=_HARNESSES,
    platforms=_PLATFORMS,
)
_BASE_URL: str = "https://litellm-dev.example.com"


# ── render: origin overrides ────────────────────────────────────────────────


def test_render_substitutes_base_url() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert _BASE_URL in script
    assert "{{ base_url }}" not in script


def test_render_substitutes_cc_switch_version() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert 'CC_SWITCH_VERSION="5.11.1"' in script
    assert "{{ cc_switch_version }}" not in script


def test_render_downloads_from_versioned_mirror_path() -> None:
    """The binary comes from the immutable versioned mirror path, not upstream."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "$BASE_URL/install/cc-switch/$CC_SWITCH_VERSION/$FILE" in script
    assert "github.com" not in script


def test_render_verifies_against_manifest_hash() -> None:
    """Integrity source of truth is the manifest sha256, not a checksums file."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "sha256sum -c" in script
    assert "checksums.txt" not in script


def test_render_has_key_diagnostic() -> None:
    """The key is validated against /v1/models before any harness is touched."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "Authorization: Bearer $API_KEY" in script
    assert "$BASE_URL/v1/models" in script


def test_render_key_via_config_file_umask() -> None:
    """The raw-config path writes the provider JSON under umask 077."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "umask 077" in script
    assert "--config-file" in script


def test_render_is_generic_orchestrator() -> None:
    """The script embeds no per-harness JSON shape; it fetches presets and composes with jq."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    # Fetches the pinned preset path and verifies its sha256
    assert "$BASE_URL/install/$preset_path" in script
    assert "preset checksum mismatch" in script
    # Composes with jq, not sed/string substitution
    assert ".options.baseURL = $url" in script
    assert ".options.apiKey = $key" in script
    # Harness list is manifest-driven
    assert ".harnesses | keys" in script
    # No hardcoded harness list
    assert 'HARNESSES="claude codex' not in script
    # mode drives switch vs additive
    assert '[ "$mode" = "switch" ]' in script


def test_render_reconcile_is_delete_then_add() -> None:
    """provider add is not idempotent, so the reconcile is delete-then-add."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "config backup" in script
    assert "provider delete" in script
    assert "provider add" in script
    assert 'use "$PROVIDER_ID"' in script


def test_render_no_upstream_urls() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    for upstream in ("github.com", "pypi.org", "astral.sh"):
        assert upstream not in script, f"upstream URL leaked: {upstream}"


def test_render_deterministic() -> None:
    assert render_install_script(_MANIFEST, _BASE_URL) == render_install_script(_MANIFEST, _BASE_URL)


def test_render_different_base_url() -> None:
    a: str = render_install_script(_MANIFEST, "https://a.example.com")
    b: str = render_install_script(_MANIFEST, "https://b.example.com")
    assert a != b
    assert "https://a.example.com" in a
    assert "https://b.example.com" in b


# ── manifest_from_json (schema 4) ───────────────────────────────────────────


def test_manifest_from_json_valid() -> None:
    raw: str = (
        '{"cc_switch_version": "5.11.1", '
        '"cc_switch_artifacts": {"linux-x64-musl": {"file": "f.tgz", "sha256": "abc"}}, '
        '"harnesses": {"open-code": {"version": 3, "path": "providers/oicm/open-code/v3.json", '
        '"sha256": "def", "mode": "additive"}}, '
        '"platforms": [{"target": "linux-x64-musl", "archive_ext": "tar.gz"}]}'
    )
    m: MirrorManifest = manifest_from_json(raw)
    assert m.cc_switch_version == "5.11.1"
    assert m.cc_switch_artifacts["linux-x64-musl"]["file"] == "f.tgz"
    assert m.platforms == (PlatformTarget(target="linux-x64-musl", archive_ext="tar.gz"),)
    assert m.harnesses == (
        HarnessPreset(
            harness="open-code",
            version=3,
            path="providers/oicm/open-code/v3.json",
            sha256="def",
            mode="additive",
        ),
    )


def test_manifest_from_json_harnesses_optional() -> None:
    raw: str = (
        '{"cc_switch_version": "5.11.1", '
        '"cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}}'
    )
    m: MirrorManifest = manifest_from_json(raw)
    assert m.harnesses == ()


def test_manifest_from_json_harness_missing_path() -> None:
    raw: str = (
        '{"cc_switch_version": "5.11.1", '
        '"cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}, '
        '"harnesses": {"open-code": {"version": 3, "sha256": "x"}}}'
    )
    with pytest.raises(ValueError, match="path"):
        manifest_from_json(raw)


def test_baked_preset_known_and_unknown() -> None:
    assert get_baked_preset("open-code") is not None
    assert "@ai-sdk/openai-compatible" in (get_baked_preset("open-code") or "")
    assert get_baked_preset("no-such-harness") is None


def test_manifest_from_json_platforms_optional() -> None:
    raw: str = (
        '{"cc_switch_version": "5.11.1", '
        '"cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}}'
    )
    m: MirrorManifest = manifest_from_json(raw)
    assert m.platforms == ()


def test_manifest_from_json_missing_version() -> None:
    with pytest.raises(ValueError, match="cc_switch_version"):
        manifest_from_json('{"cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}}')


def test_manifest_from_json_missing_artifacts() -> None:
    with pytest.raises(ValueError, match="cc_switch_artifacts"):
        manifest_from_json('{"cc_switch_version": "5.11.1"}')


def test_manifest_from_json_artifact_missing_sha() -> None:
    raw: str = '{"cc_switch_version": "5.11.1", "cc_switch_artifacts": {"t": {"file": "f"}}}'
    with pytest.raises(ValueError, match="sha256"):
        manifest_from_json(raw)


def test_manifest_from_json_invalid_json() -> None:
    with pytest.raises(ValueError, match="invalid manifest JSON"):
        manifest_from_json("not json")


# ── Jinja2 rendering contract ───────────────────────────────────────────────


def test_render_undefined_variable_raises() -> None:
    with pytest.raises(UndefinedError):
        render_install_script(_MANIFEST, _BASE_URL, template="gateway={{ no_such_var }}")


def test_render_does_not_html_escape_shell() -> None:
    script: str = render_install_script(
        _MANIFEST, _BASE_URL, template="run if a > b && c < d; {{ base_url }}"
    )
    assert "a > b && c < d" in script
    assert "&gt;" not in script
    assert "&amp;" not in script


def test_render_loop_over_platforms() -> None:
    template: str = "{% for p in platforms %}{{ p.target }}={{ p.archive_ext }} {% endfor %}"
    script: str = render_install_script(_MANIFEST, _BASE_URL, template=template)
    assert "linux-x64-musl=tar.gz" in script
    assert "windows-x64=zip" in script


def test_render_open_context_exposes_extra_manifest_keys() -> None:
    raw: str = (
        '{"cc_switch_version": "5.11.1", '
        '"cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}, '
        '"support_url": "https://help.example.com"}'
    )
    m: MirrorManifest = manifest_from_json(raw)
    script: str = render_install_script(m, _BASE_URL, template="help={{ support_url }}")
    assert script == "help=https://help.example.com"
