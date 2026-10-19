"""Tests for the install-script generator (cc-switch design)."""

from __future__ import annotations

import json
import re

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
        version=1,
        path="providers/adeo/open-code/v1.json",
        sha256="d" * 64,
    ),
    HarnessPreset(
        harness="claude",
        version=1,
        path="providers/adeo/claude/v1.json",
        sha256="e" * 64,
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


def test_render_version_probe_matches_full_version_token() -> None:
    """The skip probe compares the full version token, so fork version strings
    like jya0-5.11.1 match the manifest pin; a numeric grep would truncate them
    to 5.11.1 and skip the upgrade to the fork build."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "awk '{print $2}'" in script
    assert "grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+'" not in script


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
    """The key is validated against /api/v1/models before any harness is touched."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "Authorization: Bearer $API_KEY" in script
    assert "$BASE_URL/api/v1/models" in script
    # /v1/models carries no context_length; using it would break the limits
    assert "$BASE_URL/v1/models" not in script


def test_render_key_via_config_file_umask() -> None:
    """The raw-config path writes the provider JSON under umask 077."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "umask 077" in script
    assert "--config-file" in script


def test_render_prompts_read_from_tty_not_stdin() -> None:
    """Both interactive prompts read /dev/tty, never stdin.

    Under `curl ... | sh` stdin IS the script itself, so a plain `read` would
    silently consume the next script line as the answer (observed live: the
    key prompt consumed script text, sent it as a Bearer token, and every
    install from the pipe failed with 401 before the user could type).
    """
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert 'read -r API_KEY < "$TTY_INPUT"' in script
    assert 'read -r PICKS < "$TTY_INPUT"' in script
    # No prompt may read bare stdin
    assert "IFS= read -r API_KEY\n" not in script
    assert "IFS= read -r PICKS\n" not in script
    # Piped-mode users without a terminal get a clear error up front
    assert "run it from a terminal" in script


def test_render_key_input_is_visible() -> None:
    """The key prompt echoes what the user types.

    Hidden input (stty -echo) made it impossible to verify a pasted key
    before submitting it; a wrong paste only surfaced later as a 401.
    The prompt is a plain visible read now.
    """
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "stty -echo" not in script
    assert "stty echo" not in script


def test_render_prompts_precede_all_downloads() -> None:
    """Both prompts appear before any installer download begins.

    The original order (install jq + cc-switch, then ask) made the user
    stare at a multi-minute download before being asked anything; a wrong
    key was only discovered after the full install. Prompts come first,
    everything downloadable is deferred.
    """
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    key_prompt: int = script.index("Gateway API key")
    harness_menu: int = script.index("Which harnesses")
    jq_install: int = script.index("Installing jq")
    ccs_install: int = script.index("Installing cc-switch")
    assert key_prompt < harness_menu < jq_install < ccs_install
    # The key is validated (curl) at prompt time, before any download
    assert script.index("that key was rejected") < jq_install
    # The jq-based model count check runs after the jq bootstrap
    assert script.index("Installing jq") < script.index("can call no models")


def test_render_appends_path_export_to_rc_files() -> None:
    """The script auto-configures PATH for future shells.

    A child process cannot export into the parent shell, so the current
    shell keeps one manual export. But the script must append an
    idempotent, marker-guarded PATH export to the user's rc files so every
    future shell works with no manual setup.
    """
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "adeo-litellm installer: cc-switch + jq on PATH" in script
    # Idempotent: guarded by a marker, appended with >>, never clobbered
    assert "grep -qF" in script
    assert ">> " in script
    # Covers future shells automatically; current shell gets the one manual line
    assert "takes effect in new shells" in script
    assert "For THIS shell" in script
    # Writes to rc files, not just advice
    assert ".bashrc" in script
    assert ".profile" in script


def test_render_is_generic_orchestrator() -> None:
    """The script embeds no per-harness JSON shape; it fetches presets and composes with jq."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    # Fetches the pinned preset path and verifies its sha256
    assert "$BASE_URL/install/$preset_path" in script
    assert "preset checksum mismatch" in script
    # One generic sentinel compose; no per-harness key knowledge
    assert "__BASE_URL__" in script
    assert "__API_KEY__" in script
    assert "walk(" in script
    assert ".options.baseURL = $url" not in script
    # Per-model context windows from the key-scoped endpoint
    assert "context_length" in script
    assert "__DEFAULT_MODEL_CONTEXT__" in script
    assert "__MAX_OUTPUT_TOKENS__" in script
    assert '__MODELS_ARRAY_CTX__"' in script
    assert '"__MODELS_ARRAY_CTX_SNAKE__"' in script
    assert '__MODELS_OBJECT_LIMIT__"' in script
    assert "__MODELS_ARRAY__" not in script
    assert "__MODELS_OBJECT__" not in script
    # Manifest-sourced default output cap
    assert 'MAX_OUTPUT_TOKENS="65536"' in script
    # Harness list is manifest-driven: rendered from the context at render
    # time (the menu must appear before jq is bootstrapped, so it cannot
    # parse the fetched manifest). Adding a harness is still a store sync.
    # Sorted alphabetically so menu numbers stay stable across syncs.
    assert 'HARNESSES="claude open-code "' in script
    # No mode branch
    assert '"$mode" = "switch"' not in script
    # No sqlite3 anywhere: reconcile is pure CLI (add + use)
    assert "sqlite3" not in script
    assert "provider delete" not in script
    # Stable key-derived provider id: last 4 chars of the key, sanitized to
    # cc-switch's id charset (lowercase alnum + single dashes)
    assert "adeo-litellm-$(printf" in script
    assert 'sha256sum "$TMP/provider.json"' not in script
    # use is the reconcile gate
    assert 'use "$PROVIDER_ID" --app' in script


def test_render_reconcile_is_add_then_use() -> None:
    """No delete, no sqlite3: overwrite-add (fork flag) + use is the reconcile."""
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "config backup" in script
    assert "provider add" in script
    assert "--overwrite" in script
    assert "provider delete" not in script
    assert 'use "$PROVIDER_ID" --app' in script
    # The id is derived from the key's last 4 chars, so a re-run with the
    # same key refreshes the same provider in place (true overwrite), and a
    # rotated key maps to a different id.
    assert "adeo-litellm-$(printf" in script
    assert 'sha256sum "$TMP/provider.json"' not in script


def test_render_no_upstream_urls() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    for upstream in ("github.com", "pypi.org", "astral.sh"):
        assert upstream not in script, f"upstream URL leaked: {upstream}"


def test_render_bootstraps_jq_from_store() -> None:
    """Missing jq is bootstrapped from the gateway store, not a hard die.

    A fresh ubuntu container ships neither jq nor the desire to install it
    by hand; the installer must work with only curl present. The jq pins
    (version, per-platform file, sha256) live as literals in the script:
    reading them from the manifest would already require jq.
    """
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    # The old unconditional die is gone
    assert "command -v jq   >/dev/null 2>&1 || die" not in script
    # Bootstrap path: versioned store URL + checksum verification
    assert 'JQ_VERSION="1.8.2"' in script
    assert '"$BASE_URL/install/jq/$JQ_VERSION/$JQ_FILE"' in script
    assert "checksum mismatch for jq" in script
    # The binary lands next to cc-switch and every later call routes through it
    assert 'JQ_BIN="$INSTALL_DIR/jq"' in script
    # x86_64 Linux pin (the platform we test on) is the official 1.8.2 digest
    assert (
        'Linux-x86_64)   JQ_FILE="jq-linux-amd64";     JQ_SHA256='
        '"b1c22172dd303f3be49e935aa56aa48a8b7a46e0bc838b4997d3bb451495870f"'
    ) in script
    # Unsupported platforms still get a human fix, not a raw failure
    assert "'apt-get install jq'" in script


def test_render_jq_callsites_use_bootstrapped_binary() -> None:
    """Every jq invocation routes through $JQ_BIN, never a bare `jq`.

    A bare call would resolve via PATH and die on the exact fresh system the
    bootstrap exists for. Quoted display strings (header/info messages) are
    blanked first so `header "Installing jq ..."` is not counted as a call.
    """
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    for raw_line in script.splitlines():
        line: str = re.sub(r'"[^"]*"', '""', raw_line)  # blank out quoted strings
        stripped: str = line.strip()
        if stripped.startswith("#") or "command -v jq" in line:
            continue
        tokens: list[str] = stripped.split()
        if "jq" in tokens or stripped.startswith("jq ") or stripped.startswith("jq \\"):
            assert "$JQ_BIN" in line, f"bare jq call: {raw_line}"


def test_render_tls_failure_names_ca_fix() -> None:
    """curl exits 60/77 mean no trusted CA roots; the script names the package.

    A CA bundle cannot be fetched over TLS that itself needs the bundle, so
    the only honest remediation is telling the user what to install.
    """
    script: str = render_install_script(_MANIFEST, _BASE_URL)
    assert "60|77)" in script
    assert "ca-certificates" in script
    # The exit code must be captured with `|| RC=$?`; `if ! curl` inverts $?
    assert "if ! curl -fsSL" not in script


def test_render_deterministic() -> None:
    assert render_install_script(_MANIFEST, _BASE_URL) == render_install_script(_MANIFEST, _BASE_URL)


def test_render_different_base_url() -> None:
    a: str = render_install_script(_MANIFEST, "https://a.example.com")
    b: str = render_install_script(_MANIFEST, "https://b.example.com")
    assert a != b
    assert "https://a.example.com" in a
    assert "https://b.example.com" in b


# ── manifest_from_json (schema 5) ───────────────────────────────────────────


def test_manifest_from_json_valid() -> None:
    raw: str = (
        '{"cc_switch_version": "5.11.1", '
        '"cc_switch_artifacts": {"linux-x64-musl": {"file": "f.tgz", "sha256": "abc"}}, '
        '"harnesses": {"open-code": {"version": 1, "path": "providers/adeo/open-code/v1.json", '
        '"sha256": "def"}}, '
        '"platforms": [{"target": "linux-x64-musl", "archive_ext": "tar.gz"}]}'
    )
    m: MirrorManifest = manifest_from_json(raw)
    assert m.cc_switch_version == "5.11.1"
    assert m.cc_switch_artifacts["linux-x64-musl"]["file"] == "f.tgz"
    assert m.platforms == (PlatformTarget(target="linux-x64-musl", archive_ext="tar.gz"),)
    assert m.harnesses == (
        HarnessPreset(
            harness="open-code",
            version=1,
            path="providers/adeo/open-code/v1.json",
            sha256="def",
        ),
    )


def test_manifest_from_json_harnesses_optional() -> None:
    raw: str = '{"cc_switch_version": "5.11.1", "cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}}'
    m: MirrorManifest = manifest_from_json(raw)
    assert m.harnesses == ()


def test_manifest_from_json_harness_missing_path() -> None:
    raw: str = (
        '{"cc_switch_version": "5.11.1", '
        '"cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}, '
        '"harnesses": {"open-code": {"version": 1, "sha256": "x"}}}'
    )
    with pytest.raises(ValueError, match="path"):
        manifest_from_json(raw)


# ── baked presets: canonical per-harness shapes ──────────────────────────


def test_baked_preset_known_and_unknown() -> None:
    assert get_baked_preset("open-code") is not None
    assert get_baked_preset("no-such-harness") is None


def test_all_seven_harnesses_have_baked_presets() -> None:
    for harness in ("claude", "codex", "gemini", "open-code", "pi", "hermes", "open-claw"):
        assert get_baked_preset(harness) is not None, harness


def test_baked_preset_claude_shape() -> None:
    """claude: env block with the role-model slots + the two limit keys."""
    preset: dict[str, object] = json.loads(get_baked_preset("claude") or "{}")
    env: dict[str, str] = preset["env"]
    assert env["ANTHROPIC_AUTH_TOKEN"] == "__API_KEY__"
    assert env["ANTHROPIC_BASE_URL"] == "__BASE_URL__"
    assert env["ANTHROPIC_MODEL"] == "__DEFAULT_MODEL__"
    assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "__DEFAULT_MODEL__"
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "__DEFAULT_MODEL_CONTEXT__"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "__MAX_OUTPUT_TOKENS__"


def test_baked_preset_codex_shape() -> None:
    """codex: TOML config string + auth block, matching cc-switch's stored shape."""
    preset: dict[str, object] = json.loads(get_baked_preset("codex") or "{}")
    assert "model_provider" in str(preset["config"])
    # Bare sentinel: composed as a TOML integer, not a quoted string
    assert "model_context_window = __DEFAULT_MODEL_CONTEXT__\n" in str(preset["config"])
    assert preset["auth"] == {"OPENAI_API_KEY": "__API_KEY__"}


def test_baked_preset_gemini_shape() -> None:
    """gemini: env block with GEMINI_* keys."""
    preset: dict[str, object] = json.loads(get_baked_preset("gemini") or "{}")
    assert set(preset["env"]) == {"GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "GEMINI_MODEL"}


def test_baked_preset_models_sentinels() -> None:
    """Each preset embeds the models placeholder matching its harness's shape."""
    oc: dict[str, object] = json.loads(get_baked_preset("open-code") or "{}")
    assert list(oc["models"]) == ["__MODELS_OBJECT_LIMIT__"]
    for harness in ("pi", "open-claw"):
        p: dict[str, object] = json.loads(get_baked_preset(harness) or "{}")
        assert p["models"] == ["__MODELS_ARRAY_CTX__"], harness
    hermes: dict[str, object] = json.loads(get_baked_preset("hermes") or "{}")
    assert hermes["models"] == ["__MODELS_ARRAY_CTX_SNAKE__"]


def test_baked_preset_pi_has_flat_base_url() -> None:
    """pi reads flat baseUrl at the provider level, not options.baseURL."""
    p: dict[str, object] = json.loads(get_baked_preset("pi") or "{}")
    assert p["baseUrl"] == "__BASE_URL_V1__"
    assert "options" not in p


def test_manifest_from_json_platforms_optional() -> None:
    raw: str = '{"cc_switch_version": "5.11.1", "cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}}'
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


def test_manifest_max_output_tokens_default() -> None:
    """Omitting max_output_tokens falls back to the 64K default."""
    raw: str = '{"cc_switch_version": "5.11.1", "cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}}'
    m: MirrorManifest = manifest_from_json(raw)
    assert m.max_output_tokens == 65536
    script: str = render_install_script(m, _BASE_URL)
    assert 'MAX_OUTPUT_TOKENS="65536"' in script


def test_manifest_max_output_tokens_override() -> None:
    """An explicit max_output_tokens flows into the rendered script."""
    raw: str = (
        '{"cc_switch_version": "5.11.1", '
        '"cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}, '
        '"max_output_tokens": 32768}'
    )
    m: MirrorManifest = manifest_from_json(raw)
    assert m.max_output_tokens == 32768
    script: str = render_install_script(m, _BASE_URL)
    assert 'MAX_OUTPUT_TOKENS="32768"' in script


@pytest.mark.parametrize("bad", [0, -1, "x", 1.5, True])
def test_manifest_max_output_tokens_rejected(bad: object) -> None:
    raw: str = (
        '{"cc_switch_version": "5.11.1", '
        '"cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}, '
        '"max_output_tokens": ' + json.dumps(bad) + "}"
    )
    with pytest.raises(ValueError, match="max_output_tokens"):
        manifest_from_json(raw)


# ── Jinja2 rendering contract ───────────────────────────────────────────────


def test_render_undefined_variable_raises() -> None:
    with pytest.raises(UndefinedError):
        render_install_script(_MANIFEST, _BASE_URL, template="gateway={{ no_such_var }}")


def test_render_does_not_html_escape_shell() -> None:
    script: str = render_install_script(_MANIFEST, _BASE_URL, template="run if a > b && c < d; {{ base_url }}")
    assert "a > b && c < d" in script
    assert "&gt;" not in script
    assert "&amp;" not in script


def test_render_loop_over_platforms() -> None:
    template: str = "{% for p in platforms %}{{ p.target }}={{ p.archive_ext }} {% endfor %}"
    script: str = render_install_script(_MANIFEST, _BASE_URL, template=template)
    assert "linux-x64-musl=tar.gz" in script
    assert "windows-x64=zip" in script


def test_render_undeclared_manifest_key_not_in_context() -> None:
    raw: str = (
        '{"cc_switch_version": "5.11.1", '
        '"cc_switch_artifacts": {"t": {"file": "f", "sha256": "s"}}, '
        '"support_url": "https://help.example.com"}'
    )
    m: MirrorManifest = manifest_from_json(raw)
    with pytest.raises(UndefinedError):
        render_install_script(m, _BASE_URL, template="help={{ support_url }}")
