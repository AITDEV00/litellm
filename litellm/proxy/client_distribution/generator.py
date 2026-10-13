"""Pure install-script generator for /install.

Takes the mirror manifest and the gateway's public base URL, returns the
POSIX shell script text. No FastAPI imports; this is a pure function so it
can be unit-tested without an app instance.

The template is a lightly-modified fork of upstream ``scripts/install-cli.sh``
with three origin modifications (see docs/client-distribution/installer-script.md):

1. uv bootstrap downloads from the gateway mirror, not astral.sh.
2. ``uv tool install`` uses ``--no-index --find-links`` against the gateway
   wheelhouse, never PyPI.
3. After install, ``lite config set base_url`` pre-configures the gateway.

The template can be overridden from the object store so it can be iterated
on without a gateway rebuild. The baked-in ``_TEMPLATE`` is the fallback
when the store copy is missing or unreadable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final

# Placeholders substituted into the template. Chosen to not collide with any
# shell syntax or upstream content.
_PLACEHOLDER_BASE_URL: Final = "{{BASE_URL}}"
_PLACEHOLDER_UV_VERSION: Final = "{{UV_VERSION}}"
_PLACEHOLDER_LITELLM_CLI_VERSION: Final = "{{LITELLM_CLI_VERSION}}"

_TEMPLATE: Final = r"""#!/usr/bin/env bash
# LiteLLM CLI Installer (the thin `lite` client)
# Served by {{BASE_URL}} — installs litellm[cli] entirely from the gateway
# mirror. No external egress required.
set -eu

UV_VERSION="{{UV_VERSION}}"
LITELLM_CLI_VERSION="{{LITELLM_CLI_VERSION}}"
BASE_URL="{{BASE_URL}}"

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
MANIFEST="$(curl -fsSL "$BASE_URL/install/manifest.json")" \
  || die "Failed to fetch manifest from $BASE_URL/install/manifest.json"

# ── OS detection ───────────────────────────────────────────────────────────
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

# ── resume state ──────────────────────────────────────────────────────────
STATE_DIR="$HOME/.litellm"
STATE_FILE="$STATE_DIR/install_state.json"
mkdir -p "$STATE_DIR"

_read_state() {
  [ -f "$STATE_FILE" ] && cat "$STATE_FILE" || echo '{}'
}

_write_state() {
  printf '{"uv_version":"%s","lite_version":"%s","base_url":"%s"}\n' \
    "$1" "$2" "$3" > "$STATE_FILE"
}

# ── Phase A: acquire ──────────────────────────────────────────────────────
STATE="$(_read_state)"
STATE_UV="$(printf '%s' "$STATE" | grep -o '"uv_version":"[^"]*"' | cut -d'"' -f4 || true)"
STATE_LITE="$(printf '%s' "$STATE" | grep -o '"lite_version":"[^"]*"' | cut -d'"' -f4 || true)"
STATE_URL="$(printf '%s' "$STATE" | grep -o '"base_url":"[^"]*"' | cut -d'"' -f4 || true)"

NEED_UV=1
NEED_LITE=1
NEED_CONFIG=1

# A1: uv present and version matches
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

if [ -n "$UV_BIN" ]; then
  CURRENT_UV_VERSION="$("$UV_BIN" --version 2>/dev/null | awk '{print $2}' | head -1 || true)"
  if [ "$CURRENT_UV_VERSION" = "$UV_VERSION" ] && [ "$STATE_UV" = "$UV_VERSION" ]; then
    NEED_UV=0
    info "uv $UV_VERSION already installed, skipping"
  fi
fi

# A2: lite present and version matches
LITE_BIN=""
SCRIPTS_DIR=""
if [ -n "$UV_BIN" ]; then
  SCRIPTS_DIR="$("$UV_BIN" tool dir --bin 2>/dev/null || true)"
  if [ -n "$SCRIPTS_DIR" ] && [ -x "$SCRIPTS_DIR/lite" ]; then
    LITE_BIN="$SCRIPTS_DIR/lite"
    CURRENT_LITE_VERSION="$("$LITE_BIN" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || true)"
    if [ "$CURRENT_LITE_VERSION" = "$LITELLM_CLI_VERSION" ] && [ "$STATE_LITE" = "$LITELLM_CLI_VERSION" ]; then
      NEED_LITE=0
      info "lite $LITELLM_CLI_VERSION already installed, skipping"
    fi
  fi
fi

# A3: base_url already configured
if [ "$STATE_URL" = "$BASE_URL" ] && [ -n "$LITE_BIN" ]; then
  EXISTING_URL="$("$LITE_BIN" config get base_url 2>/dev/null || true)"
  if [ "$EXISTING_URL" = "$BASE_URL" ]; then
    NEED_CONFIG=0
    info "base_url already set to $BASE_URL, skipping"
  fi
fi

# ── A1: install/upgrade uv ────────────────────────────────────────────────
if [ "$NEED_UV" -eq 1 ]; then
  header "Installing uv $UV_VERSION..."
  export INSTALLER_DOWNLOAD_URL="$BASE_URL/install/uv/$UV_VERSION"
  curl -fsSL "$BASE_URL/install/uv/$UV_VERSION/uv-installer.sh" \
    | env INSTALLER_DOWNLOAD_URL="$INSTALLER_DOWNLOAD_URL" UV_NO_MODIFY_PATH=1 sh \
    || die "uv installation failed from mirror"
  UV_BIN="$HOME/.local/bin/uv"
fi

# ── A2: install/upgrade lite ──────────────────────────────────────────────
if [ "$NEED_LITE" -eq 1 ]; then
  echo ""
  header "Installing litellm[cli] $LITELLM_CLI_VERSION..."
  echo ""
  export UV_PYTHON_INSTALL_MIRROR="$BASE_URL/install/python"
  "$UV_BIN" tool install \
    --python '>=3.10,<3.15' --python-preference system --force \
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

# ── A3: configure base_url ────────────────────────────────────────────────
if [ "$NEED_CONFIG" -eq 1 ]; then
  "$LITE_BIN" config set base_url "$BASE_URL"
fi

# Record state
_write_state "$UV_VERSION" "$LITELLM_CLI_VERSION" "$BASE_URL"

# ── success ────────────────────────────────────────────────────────────────
echo ""
success "LiteLLM CLI installed"
info "Version: $LITELLM_CLI_VERSION"

# ── harness detection (report only) ───────────────────────────────────────
echo ""
header "Detected harnesses:"
for harness in claude codex opencode pi; do
  if command -v "$harness" >/dev/null 2>&1; then
    info "  $harness    found"
  else
    info "  $harness    not found"
  fi
done
echo ""

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
class MirrorManifest:
    """The fields from manifest.json the generator needs."""

    uv_version: str
    litellm_cli_version: str
    python_version: str


def render_install_script(
    manifest: MirrorManifest,
    base_url: str,
    template: str = _TEMPLATE,
) -> str:
    """Render the /install script from the manifest and gateway base URL.

    Pure function: same inputs always produce the same output, no I/O.
    ``template`` defaults to the baked-in version; pass a store-fetched
    string to override it.
    """
    script: str = template
    script = script.replace(_PLACEHOLDER_BASE_URL, base_url)
    script = script.replace(_PLACEHOLDER_UV_VERSION, manifest.uv_version)
    script = script.replace(_PLACEHOLDER_LITELLM_CLI_VERSION, manifest.litellm_cli_version)
    return script


def get_template() -> str:
    """Return the current install-script template.

    This is the pure-function entry point used by tests. For the store-aware
    version, see ``routes._fetch_template``.
    """
    return _TEMPLATE


def manifest_from_json(raw: str) -> MirrorManifest:
    """Parse a manifest.json string into a MirrorManifest.

    Raises ``ValueError`` on missing or malformed fields.
    """
    try:
        data: dict[str, object] = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid manifest JSON: {exc}") from exc

    uv_version: object = data.get("uv_version")
    cli_version: object = data.get("litellm_cli_version")
    python_version: object = data.get("python_version")

    if not isinstance(uv_version, str) or not uv_version:
        raise ValueError("manifest missing 'uv_version'")
    if not isinstance(cli_version, str) or not cli_version:
        raise ValueError("manifest missing 'litellm_cli_version'")
    if not isinstance(python_version, str) or not python_version:
        raise ValueError("manifest missing 'python_version'")

    return MirrorManifest(
        uv_version=uv_version,
        litellm_cli_version=cli_version,
        python_version=python_version,
    )
