"""Pure install-script generator for /install.

Takes the mirror manifest and the gateway's public base URL, returns the
POSIX shell script text. No FastAPI imports; this is a pure function so it
can be unit-tested without an app instance.

The template is a Jinja2 document rendered with a structured context
(``RenderContext``), so the script can grow conditionals and loops without
the renderer changing. Three origin modifications over upstream
``scripts/install-cli.sh`` (see docs/client-distribution/installer-script.md):

1. uv bootstrap downloads from the gateway mirror, not astral.sh.
2. ``uv tool install`` uses ``--no-index --find-links`` against the gateway
   wheelhouse, never PyPI.
3. After install, ``lite config set base_url`` pre-configures the gateway.

Rendering is sandboxed + strict + no-autoescape, with an **open context**:

every key in the manifest is exposed to the template, plus ``base_url`` and a
typed ``platforms`` list. Adding a manifest field makes it usable in the
template with no code change or rebuild — only a RustFS sync.

- ``StrictUndefined`` turns a reference to a key the manifest does not carry
  into a hard render error instead of a silently broken installer.
- ``SandboxedEnvironment`` bounds what a store-pushed template can reach.
- ``autoescape=False`` keeps shell metacharacters (``&``, ``<``, ``>``) intact.

The template can be overridden from the object store so it can be iterated
on without a gateway rebuild. The baked-in ``_TEMPLATE`` is the fallback
when the store copy is missing or unreadable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Final

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

# The single render environment. autoescape is off (this is a shell script,
# not HTML), undefined variables are a hard error, and the sandbox bounds
# attribute access from store-pushed templates.
_ENV: Final = SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)

_TEMPLATE: Final = r"""#!/usr/bin/env bash
# LiteLLM CLI Installer (the thin `lite` client)
# Served by {{ base_url }} — installs litellm[cli] entirely from the gateway
# mirror. No external egress required.
set -eu

UV_VERSION="{{ uv_version }}"
LITELLM_CLI_VERSION="{{ litellm_cli_version }}"
PYTHON_VERSION="{{ python_version }}"
PYTHON_BUILD_TAG="{{ python_build_tag }}"
BASE_URL="{{ base_url }}"

# ── colours ────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  BOLD='\033[1m'
  GREEN='\033[38;2;78;186;101m'
  GREY='\033[38;2;153;153;153m'
  RESET='\033[0m'
else
  BOLD='' GREEN='' GREY='' RESET=''
fi

info()    { printf "${GREY}  %s${RESET}\n" "$*"; }
success() { printf "${GREEN}  ✔ %s${RESET}\n" "$*"; }
header()  { printf "${BOLD}  %s${RESET}\n" "$*"; }
die()     { printf "\n  Error: %s\n\n" "$*" >&2; exit 1; }

# ── banner ─────────────────────────────────────────────────────────────────
echo ""
cat << 'BANNER'
  ██╗     ██║████████╗███████╗
  ██║     ██║╚══██╔══╝██╔════╝
  ██║     ██║   ██║   █████╗
  ██║     ██║   ██║   ██╔══╝
  ███████╗██║   ██║   ███████╗
  ╚══════╝╚═╝   ╚═╝   ╚══════╝
BANNER
printf "  ${BOLD}LiteLLM CLI Installer${RESET}  ${GREY}the thin 'lite' client for your proxy${RESET}\n\n"
info "Gateway: $BASE_URL"
info "CLI version: $LITELLM_CLI_VERSION"

# ── fetch manifest ────────────────────────────────────────────────────────
curl -fsSL "$BASE_URL/install/manifest.json" >/dev/null \
  || die "Failed to fetch manifest from $BASE_URL/install/manifest.json"

# ── platform detection ─────────────────────────────────────────────────────
# Map this machine onto one of the targets the mirror carries. The platform
# set is data-driven from the manifest (see generator.RenderContext.platforms),
# so adding a target is a manifest change, not a script change.
OS="$(uname -s)"
ARCH="$(uname -m)"

# Map MINGW/MSYS (Git Bash) to Windows artifacts
case "$OS" in
  MINGW*|MSYS*) OS="Windows" ;;
esac

case "$OS" in
  Darwin)  PLATFORM="macOS ($ARCH)" ;;
  Linux)   PLATFORM="Linux ($ARCH)" ;;
  Windows) PLATFORM="Windows ($ARCH)" ;;
  *)       die "Unsupported OS: $OS. LiteLLM supports macOS, Linux, and Windows (Git Bash/WSL)." ;;
esac

info "Platform: $PLATFORM"

# ── resolve existing binaries ──────────────────────────────────────────────
UV_BIN=""
for candidate in uv "$HOME/.local/bin/uv"; do
  if command -v "$candidate" >/dev/null 2>&1; then
    UV_BIN="$(command -v "$candidate")"
    break
  elif [ -x "$candidate" ]; then
    UV_BIN="$candidate"
    break
  fi
done

# ── A1: install/upgrade uv (skip if version already matches) ──────────────
if [ -n "$UV_BIN" ] && [ "$("$UV_BIN" --version 2>/dev/null | awk '{print $2}' | head -1 || true)" = "$UV_VERSION" ]; then
  info "uv $UV_VERSION already installed, skipping"
else
  header "Installing uv $UV_VERSION..."
  export INSTALLER_DOWNLOAD_URL="$BASE_URL/install/uv/$UV_VERSION"
  curl -fsSL "$BASE_URL/install/uv/$UV_VERSION/uv-installer.sh" \
    | env INSTALLER_DOWNLOAD_URL="$INSTALLER_DOWNLOAD_URL" UV_NO_MODIFY_PATH=1 sh \
    || die "uv installation failed from mirror"
  UV_BIN="$HOME/.local/bin/uv"
fi

# ── A2: install/upgrade lite (--force is idempotent) ─────────────────────
SCRIPTS_DIR="$("$UV_BIN" tool dir --bin 2>/dev/null || true)"
LITE_BIN="${SCRIPTS_DIR:+$SCRIPTS_DIR/lite}"
CURRENT_LITE_VERSION=""
if [ -n "$LITE_BIN" ] && [ -x "$LITE_BIN" ]; then
  CURRENT_LITE_VERSION="$("$LITE_BIN" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
fi

if [ "$CURRENT_LITE_VERSION" = "$LITELLM_CLI_VERSION" ]; then
  info "lite $LITELLM_CLI_VERSION already installed, skipping"
else
  echo ""
  header "Installing litellm[cli] $LITELLM_CLI_VERSION..."
  echo ""
  # Pin the exact managed Python the mirror carries. uv constructs the
  # download URL as <mirror>/<build_tag>/<archive>, so we pin both the build
  # tag and the version, and request the pinned interpreter rather than a
  # range. --python-preference only-managed forces the mirror fetch (no system
  # fallback), and --no-index keeps resolution off PyPI.
  export UV_PYTHON_INSTALL_MIRROR="$BASE_URL/install/python"
  export UV_PYTHON_CPYTHON_BUILD="$PYTHON_BUILD_TAG"
  "$UV_BIN" tool install \
    --python "$PYTHON_VERSION" --python-preference only-managed --force \
    --no-index --find-links "$BASE_URL/install/pypi" \
    "litellm[cli]==$LITELLM_CLI_VERSION" \
    || die "uv tool install failed. Check that the mirror has the wheelhouse."
fi

# Re-resolve lite binary
SCRIPTS_DIR="$("$UV_BIN" tool dir --bin)"
LITE_BIN="$SCRIPTS_DIR/lite"
if [ ! -x "$LITE_BIN" ]; then
  die "lite binary not found after install"
fi

# ── A3: configure base_url (idempotent) ──────────────────────────────────
"$LITE_BIN" config set base_url "$BASE_URL"

# ── success ────────────────────────────────────────────────────────────────
echo ""
success "LiteLLM CLI installed"
info "Version: $LITELLM_CLI_VERSION"

# ── PATH hint ──────────────────────────────────────────────────────────────
if ! command -v lite >/dev/null 2>&1; then
  info "Note: add lite to your PATH:  export PATH=\"\$PATH:$SCRIPTS_DIR\""
fi

# ── next steps ─────────────────────────────────────────────────────────────
echo ""
header "Next steps:"
echo ""
info "  lite login              # authenticate to $BASE_URL"
info "  lite configure claude   # wire Claude Code"
info "  lite claude             # run Claude Code through the proxy"
echo ""
"""


@dataclass(frozen=True, slots=True)
class PlatformTarget:
    """One platform the mirror carries artifacts for."""

    target: str  # e.g. "x86_64-unknown-linux-gnu"
    archive_ext: str  # "tar.gz" | "zip"


@dataclass(frozen=True, slots=True)
class MirrorManifest:
    """The fields from manifest.json the generator needs (schema 2)."""

    uv_version: str
    litellm_cli_version: str
    python_version: str
    python_build_tag: str
    platforms: tuple[PlatformTarget, ...] = field(default=())
    extra: dict[str, object] = field(default_factory=dict)

    def to_context(self, base_url: str) -> dict[str, object]:
        """Build the Jinja2 render context for this manifest + gateway.

        Open context: every declared field plus any extra manifest keys are
        exposed by name, so a template can reference new manifest data without
        a code change. ``platforms`` is exposed as typed PlatformTarget
        objects (``p.target`` / ``p.archive_ext``); everything else is the raw
        manifest value.
        """
        return {
            **self.extra,
            "base_url": base_url,
            "uv_version": self.uv_version,
            "litellm_cli_version": self.litellm_cli_version,
            "python_version": self.python_version,
            "python_build_tag": self.python_build_tag,
            "platforms": self.platforms,
        }


def render_install_script(
    manifest: MirrorManifest,
    base_url: str,
    template: str = _TEMPLATE,
) -> str:
    """Render the /install script from the manifest and gateway base URL.

    Pure function: same inputs always produce the same output, no I/O.
    ``template`` defaults to the baked-in version; pass a store-fetched
    string to override it. Raises ``jinja2.UndefinedError`` if the template
    references a variable not in the context (StrictUndefined).
    """
    return _ENV.from_string(template).render(**manifest.to_context(base_url))


def get_template() -> str:
    """Return the current install-script template.

    This is the pure-function entry point used by tests. For the store-aware
    version, see ``routes._fetch_template``.
    """
    return _TEMPLATE


def manifest_from_json(raw: str) -> MirrorManifest:
    """Parse a manifest.json string into a MirrorManifest (schema 2).

    Raises ``ValueError`` on missing or malformed fields. ``platforms`` is a
    list of ``{"target": ..., "archive_ext": ...}`` objects; when omitted the
    manifest carries no platform metadata and the template renders an empty
    platform set.
    """
    try:
        data: dict[str, object] = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid manifest JSON: {exc}") from exc

    uv_version: object = data.get("uv_version")
    cli_version: object = data.get("litellm_cli_version")
    python_version: object = data.get("python_version")
    python_build_tag: object = data.get("python_build_tag")

    if not isinstance(uv_version, str) or not uv_version:
        raise ValueError("manifest missing 'uv_version'")
    if not isinstance(cli_version, str) or not cli_version:
        raise ValueError("manifest missing 'litellm_cli_version'")
    if not isinstance(python_version, str) or not python_version:
        raise ValueError("manifest missing 'python_version'")
    if not isinstance(python_build_tag, str) or not python_build_tag:
        raise ValueError("manifest missing 'python_build_tag'")

    declared: Final = {
        "schema",
        "uv_version",
        "litellm_cli_version",
        "python_version",
        "python_build_tag",
        "platforms",
        "uv_artifacts",
        "python_artifacts",
        "wheelhouse",
    }
    extra: dict[str, object] = {k: v for k, v in data.items() if k not in declared}

    return MirrorManifest(
        uv_version=uv_version,
        litellm_cli_version=cli_version,
        python_version=python_version,
        python_build_tag=python_build_tag,
        platforms=_platforms_from_json(data.get("platforms")),
        extra=extra,
    )


def _platforms_from_json(raw: object) -> tuple[PlatformTarget, ...]:
    """Parse the optional ``platforms`` list into PlatformTarget tuples."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("manifest 'platforms' must be a list")
    platforms: list[PlatformTarget] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError("manifest 'platforms' entries must be objects")
        target: object = entry.get("target")
        archive_ext: object = entry.get("archive_ext")
        if not isinstance(target, str) or not target:
            raise ValueError("platform entry missing 'target'")
        if not isinstance(archive_ext, str) or not archive_ext:
            raise ValueError(f"platform '{target}' missing 'archive_ext'")
        platforms.append(PlatformTarget(target=target, archive_ext=archive_ext))
    return tuple(platforms)
