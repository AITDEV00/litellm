"""Pure install-script generator for /install.

Takes the mirror manifest and the gateway's public base URL, returns the
POSIX shell script text. No FastAPI imports; this is a pure function so it
can be unit-tested without an app instance.

The template is a Jinja2 document rendered with a structured context, so the
script can grow conditionals and loops without the renderer changing. It is
a **generic orchestrator** (see docs/client-distribution/installer-script.md):

1. fetch the manifest, detect the platform, install cc-switch from the
   gateway mirror (sha256-verified against the manifest),
2. prompt for the gateway key and validate it against ``/v1/models``,
3. offer the manifest-declared harnesses for multi-select,
4. per chosen harness: fetch the manifest-pinned preset, verify its sha256,
   compose the final cc-switch config client-side with jq (never string
   substitution), upsert the provider, and activate it.

Rendering is sandboxed + strict + no-autoescape, with an **open context**:

every key in the manifest is exposed to the template, plus ``base_url`` and
the typed ``platforms`` / ``harnesses`` lists. Adding a manifest field makes
it usable in the template with no code change or rebuild — only a RustFS sync.

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
# OICM coding-agent setup (cc-switch)
# Served by {{ base_url }} — installs cc-switch and points your coding
# harnesses at the gateway. No external egress required.
set -eu

CC_SWITCH_VERSION="{{ cc_switch_version }}"
BASE_URL="{{ base_url }}"
PROVIDER_NAME="OICM LiteLLM"
PROVIDER_ID="oicm-litellm"
INSTALL_DIR="${CC_SWITCH_INSTALL_DIR:-$HOME/.local/bin}"

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
printf "  ${BOLD}OICM coding-agent setup${RESET}  ${GREY}cc-switch -> your gateway${RESET}\n\n"
info "Gateway: $BASE_URL"
info "cc-switch: $CC_SWITCH_VERSION"

command -v curl >/dev/null 2>&1 || die "curl is required but not installed."
command -v jq   >/dev/null 2>&1 || die "jq is required but not installed."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── fetch manifest (the integrity source of truth) ────────────────────────
curl -fsSL "$BASE_URL/install/manifest.json" -o "$TMP/manifest.json" \
  || die "Failed to fetch manifest from $BASE_URL/install/manifest.json"

# ── platform detection ─────────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"

# Map MINGW/MSYS (Git Bash) to the Windows archive
case "$OS" in
  MINGW*|MSYS*) OS="Windows" ;;
esac

case "$OS" in
  Linux)
    case "$ARCH" in
      x86_64)  TARGET="linux-x64-musl" ;;
      aarch64) TARGET="linux-arm64-musl" ;;
      *)       die "Unsupported Linux arch: $ARCH" ;;
    esac
    ;;
  Darwin)   TARGET="darwin-universal" ;;
  Windows)  TARGET="windows-x64" ;;
  *)        die "Unsupported OS: $OS" ;;
esac

info "Platform: $OS ($ARCH) -> $TARGET"

# ── 1. install cc-switch (skip if version matches) ────────────────────────
CCS_BIN=""
for candidate in cc-switch "$INSTALL_DIR/cc-switch"; do
  if command -v "$candidate" >/dev/null 2>&1; then CCS_BIN="$(command -v "$candidate")"; break
  elif [ -x "$candidate" ]; then CCS_BIN="$candidate"; break; fi
done

if [ -n "$CCS_BIN" ] && [ "$("$CCS_BIN" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)" = "$CC_SWITCH_VERSION" ]; then
  info "cc-switch $CC_SWITCH_VERSION already installed, skipping"
else
  header "Installing cc-switch $CC_SWITCH_VERSION..."
  FILE="$(jq -r --arg t "$TARGET" '.cc_switch_artifacts[$t].file' "$TMP/manifest.json")"
  WANT="$(jq -r --arg t "$TARGET" '.cc_switch_artifacts[$t].sha256' "$TMP/manifest.json")"
  [ "$FILE" != "null" ] && [ "$WANT" != "null" ] || die "no artifact in the manifest for $TARGET"
  curl -fsSL "$BASE_URL/install/cc-switch/$CC_SWITCH_VERSION/$FILE" -o "$TMP/ccs.tgz" \
    || die "download failed: $BASE_URL/install/cc-switch/$CC_SWITCH_VERSION/$FILE"
  echo "$WANT  $TMP/ccs.tgz" | sha256sum -c - >/dev/null \
    || die "checksum mismatch for $FILE — the store may be corrupt; contact the platform team"
  mkdir -p "$INSTALL_DIR"
  tar -xzf "$TMP/ccs.tgz" -C "$INSTALL_DIR"
  chmod +x "$INSTALL_DIR/cc-switch" 2>/dev/null || true
  CCS_BIN="$INSTALL_DIR/cc-switch"
fi
[ -x "$CCS_BIN" ] || die "cc-switch binary not found after install"

# ── 2. prompt for the gateway key + diagnostic ────────────────────────────
echo ""
header "Gateway API key"
printf "  Paste your OICM LiteLLM virtual key, then Enter: "
if [ -t 0 ]; then stty -echo 2>/dev/null || true; fi
IFS= read -r API_KEY
if [ -t 0 ]; then stty echo 2>/dev/null || true; fi
echo ""
[ -n "$API_KEY" ] || die "no key entered"

curl -fsSL -H "Authorization: Bearer $API_KEY" "$BASE_URL/v1/models" -o "$TMP/models.json" \
  || die "that key was rejected by $BASE_URL; check it with your platform admin"
MODEL_COUNT="$(jq -r '.data | length' "$TMP/models.json" 2>/dev/null || echo 0)"
[ "$MODEL_COUNT" -gt 0 ] || die "the key works but can call no models; ask your admin for model access"
info "key OK — $MODEL_COUNT model(s) available"

# ── 3. harness selection ──────────────────────────────────────────────────
# The harness list comes from the manifest, so adding a harness is a sync,
# not a script change.
HARNESSES="$(jq -r '.harnesses | keys | join(" ")' "$TMP/manifest.json")"
[ -n "$HARNESSES" ] && [ "$HARNESSES" != "null" ] || die "manifest declares no harnesses"
echo ""
header "Which harnesses should route through the gateway?"
i=1
for h in $HARNESSES; do printf "    %d) %s\n" "$i" "$h"; i=$((i+1)); done
printf "  Enter numbers separated by spaces (e.g. 1 3): "
IFS= read -r PICKS
[ -n "$PICKS" ] || die "nothing selected"

CHOSEN=""
for n in $PICKS; do
  h="$(echo "$HARNESSES" | cut -d' ' -f"$n" 2>/dev/null || true)"
  [ -n "$h" ] || die "invalid selection: $n"
  CHOSEN="$CHOSEN $h"
done

# ── 4. per-harness provider reconcile (backup + delete-then-add + use) ────
# Generic orchestrator: the per-harness cc-switch JSON shape lives in the
# versioned preset the manifest pins, not in this script. The script fetches
# the preset, verifies it against the manifest sha256, then composes the
# final config client-side with jq from runtime values (base URL, API key,
# accessible models) — never string substitution.
umask 077
MODELS_JSON="$(jq -c '[.data[].id] | map(select(test("claude|gpt|qwen|glm";"i"))) | .[0:20]' "$TMP/models.json" 2>/dev/null || echo '[]')"
DEFAULT_MODEL="$(printf '%s' "$MODELS_JSON" | jq -r '.[0] // empty' 2>/dev/null || true)"

# Write the provider's settings_config into cc-switch's store, add-or-update.
# cc-switch's `provider delete` uses an inquire::Confirm prompt that reads the
# TTY directly (never scriptable headless) and `provider edit` takes no config
# flags, so there is no CLI update path for a changed config. We add fresh via
# `provider add`; when the id already exists we UPDATE the settings_config row
# in cc-switch's SQLite DB directly (PK is (id, app_type)). `use` then
# re-points the harness and rewrites its native config from the store.
upsert_provider() {
  local harness="$1" config_file="$2"
  local db="$HOME/.cc-switch/cc-switch.db"
  # cc-switch stores app_type without the dash (open-code -> opencode).
  local app="$(printf '%s' "$harness" | tr -d '-')"
  if "$CCS_BIN" provider list --app "$harness" </dev/null 2>/dev/null | grep -q " $PROVIDER_ID "; then
    command -v sqlite3 >/dev/null 2>&1 \
      || die "$harness is already configured and updating it needs sqlite3; install sqlite3 or remove the provider in the cc-switch TUI"
    # Build the SQL as a file to avoid argv quoting issues with the JSON body:
    # compact single-line JSON, single quotes doubled for the SQL literal.
    jq -c . "$config_file" | sed "s/'/''/g" > "$TMP/cfg.txt"
    { printf "UPDATE providers SET settings_config='"; cat "$TMP/cfg.txt"; printf "' WHERE id='%s' AND app_type='%s';\n" "$PROVIDER_ID" "$app"; } > "$TMP/upd.sql"
    sqlite3 "$db" < "$TMP/upd.sql" \
      || die "failed to update $PROVIDER_ID for $harness"
    rm -f "$TMP/cfg.txt" "$TMP/upd.sql"
  else
    "$CCS_BIN" provider add --app "$harness" --id "$PROVIDER_ID" --name "$PROVIDER_NAME" \
      --config-file "$config_file" \
      >/dev/null 2>&1 || die "provider add failed for $harness"
  fi
}

configure_harness() {
  local harness="$1" mode="$2" preset_path="$3" preset_sha="$4"
  header "Configuring $harness..."
  "$CCS_BIN" config backup >/dev/null 2>&1 || true

  if [ "$mode" = "switch" ]; then
    # Field-mode harnesses: build the settings_config cc-switch would store
    # (env block with base URL + auth token + model) and upsert it.
    jq -n \
      --arg url "$BASE_URL" \
      --arg key "$API_KEY" \
      --arg model "$DEFAULT_MODEL" \
      '{env:({ANTHROPIC_BASE_URL:$url, ANTHROPIC_AUTH_TOKEN:$key}
         + (if $model != "" then {ANTHROPIC_MODEL:$model} else {} end))}' \
      > "$TMP/provider.json" || die "failed to compose config for $harness"
    upsert_provider "$harness" "$TMP/provider.json"
    rm -f "$TMP/provider.json"
  else
    # Additive harnesses take a config built from the pinned preset.
    curl -fsSL "$BASE_URL/install/$preset_path" -o "$TMP/preset.json" \
      || die "failed to fetch preset for $harness: $BASE_URL/install/$preset_path"
    echo "$preset_sha  $TMP/preset.json" | sha256sum -c - >/dev/null \
      || die "preset checksum mismatch for $harness — the store may be corrupt"

    jq \
      --arg name "$PROVIDER_NAME" \
      --arg url "$BASE_URL/v1" \
      --arg key "$API_KEY" \
      --argjson models "$MODELS_JSON" \
      '.name = $name
       | .options.baseURL = $url
       | .options.apiKey = $key
       | .models = ( $models | map({key:., value:{name:.}}) | from_entries )' \
      "$TMP/preset.json" > "$TMP/provider.json" \
      || die "failed to compose config for $harness"

    upsert_provider "$harness" "$TMP/provider.json"
    rm -f "$TMP/preset.json" "$TMP/provider.json"
  fi

  # use is the real reconcile gate: it points the harness at our provider and
  # writes the harness's native config file. This is what must succeed.
  "$CCS_BIN" use "$PROVIDER_ID" --app "$harness" </dev/null \
    || die "failed to activate $PROVIDER_ID for $harness"
  success "$harness configured"
}

for HARNESS in $CHOSEN; do
  echo ""
  MODE="$(jq -r --arg h "$HARNESS" '.harnesses[$h].mode // "additive"' "$TMP/manifest.json")"
  PPATH="$(jq -r --arg h "$HARNESS" '.harnesses[$h].path // empty' "$TMP/manifest.json")"
  PSHA="$(jq -r --arg h "$HARNESS" '.harnesses[$h].sha256 // empty' "$TMP/manifest.json")"
  # Detach stdin: curl/jq/cc-switch in the configure step would otherwise
  # consume the piped harness picks and stall waiting for input.
  configure_harness "$HARNESS" "$MODE" "$PPATH" "$PSHA" </dev/null
done

# ── done ───────────────────────────────────────────────────────────────────
echo ""
"$CCS_BIN" env check 2>/dev/null || true
echo ""
success "Done. Restart each configured harness to pick up the gateway."
info "Re-run this script any time to re-sync models:  curl -fsSL $BASE_URL/install | sh"
info "Update cc-switch only via this script, not 'cc-switch update'."
echo ""
"""


# Baked-in fallback presets for additive harnesses, keyed by cc-switch --app
# id. These are the structural skeleton only; the installer fills baseURL,
# apiKey and models from runtime values. The store-backed versioned preset
# (providers/oicm/<harness>/v<N>.json) overrides these when the manifest pins
# one — these exist so a harness works even before its first preset sync.
ADDITIVE_PRESETS: Final[dict[str, str]] = {
    "open-code": json.dumps(
        {
            "npm": "@ai-sdk/openai-compatible",
            "name": "OICM LiteLLM",
            "options": {"baseURL": "", "apiKey": ""},
            "models": {},
        },
        indent=2,
    ),
    "pi": json.dumps(
        {
            "npm": "@ai-sdk/openai-compatible",
            "name": "OICM LiteLLM",
            "options": {"baseURL": "", "apiKey": ""},
            "models": {},
        },
        indent=2,
    ),
    "hermes": json.dumps(
        {
            "npm": "@ai-sdk/openai-compatible",
            "name": "OICM LiteLLM",
            "options": {"baseURL": "", "apiKey": ""},
            "models": {},
        },
        indent=2,
    ),
    "open-claw": json.dumps(
        {
            "npm": "@ai-sdk/openai-compatible",
            "name": "OICM LiteLLM",
            "options": {"baseURL": "", "apiKey": ""},
            "models": {},
        },
        indent=2,
    ),
}


@dataclass(frozen=True, slots=True)
class PlatformTarget:
    """One platform the mirror carries artifacts for."""

    target: str  # e.g. "x86_64-unknown-linux-gnu"
    archive_ext: str  # "tar.gz" | "zip"


@dataclass(frozen=True, slots=True)
class HarnessPreset:
    """One harness's versioned cc-switch configuration slice (schema 4).

    The manifest pins each harness to an immutable, store-backed preset so a
    harness's cc-switch JSON shape can be revised without a gateway rebuild.
    """

    harness: str  # cc-switch --app id, e.g. "open-code"
    version: int
    path: str  # store key, e.g. "providers/oicm/open-code/v3.json"
    sha256: str
    mode: str  # "switch" (field-mode add) | "additive" (--config-file add)


@dataclass(frozen=True, slots=True)
class MirrorManifest:
    """The fields from manifest.json the generator needs (schema 4)."""

    cc_switch_version: str
    cc_switch_artifacts: dict[str, dict[str, str]]
    harnesses: tuple[HarnessPreset, ...] = field(default=())
    platforms: tuple[PlatformTarget, ...] = field(default=())
    extra: dict[str, object] = field(default_factory=dict)

    def to_context(self, base_url: str) -> dict[str, object]:
        """Build the Jinja2 render context for this manifest + gateway.

        Open context: every declared field plus any extra manifest keys are
        exposed by name, so a template can reference new manifest data without
        a code change. ``harnesses`` and ``platforms`` are exposed as typed
        objects; everything else is the raw manifest value.
        """
        return {
            **self.extra,
            "base_url": base_url,
            "cc_switch_version": self.cc_switch_version,
            "cc_switch_artifacts": self.cc_switch_artifacts,
            "harnesses": self.harnesses,
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


def get_baked_preset(harness: str) -> str | None:
    """Return the baked-in fallback preset for an additive harness, or None."""
    return ADDITIVE_PRESETS.get(harness)


def manifest_from_json(raw: str) -> MirrorManifest:
    """Parse a manifest.json string into a MirrorManifest (schema 3).

    Raises ``ValueError`` on missing or malformed fields. ``platforms`` is a
    list of ``{"target": ..., "archive_ext": ...}`` objects; when omitted the
    manifest carries no platform metadata and the template renders an empty
    platform set. ``harnesses`` maps a cc-switch ``--app`` id to a pinned
    preset ``{"version", "path", "sha256", "mode"}`` (schema 4).
    """
    try:
        data: dict[str, object] = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"invalid manifest JSON: {exc}") from exc

    cc_switch_version: object = data.get("cc_switch_version")
    cc_switch_artifacts: object = data.get("cc_switch_artifacts")

    if not isinstance(cc_switch_version, str) or not cc_switch_version:
        raise ValueError("manifest missing 'cc_switch_version'")
    if not isinstance(cc_switch_artifacts, dict) or not cc_switch_artifacts:
        raise ValueError("manifest missing 'cc_switch_artifacts'")

    declared: Final = {
        "schema",
        "cc_switch_version",
        "cc_switch_artifacts",
        "harnesses",
        "platforms",
    }
    extra: dict[str, object] = {k: v for k, v in data.items() if k not in declared}

    return MirrorManifest(
        cc_switch_version=cc_switch_version,
        cc_switch_artifacts=_artifacts_from_json(cc_switch_artifacts),
        harnesses=_harnesses_from_json(data.get("harnesses")),
        platforms=_platforms_from_json(data.get("platforms")),
        extra=extra,
    )


def _harnesses_from_json(raw: object) -> tuple[HarnessPreset, ...]:
    """Parse the optional ``harnesses`` map into HarnessPreset tuples."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError("manifest 'harnesses' must be an object")
    presets: list[HarnessPreset] = []
    for harness, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"harness '{harness}' preset must be an object")
        version: object = entry.get("version")
        path: object = entry.get("path")
        sha256: object = entry.get("sha256")
        mode: object = entry.get("mode", "additive")
        if not isinstance(version, int):
            raise ValueError(f"harness '{harness}' preset missing int 'version'")
        if not isinstance(mode, str) or not mode:
            raise ValueError(f"harness '{harness}' preset missing 'mode'")
        # switch-mode harnesses carry no preset file; additive ones must
        if mode == "additive":
            if not isinstance(path, str) or not path:
                raise ValueError(f"harness '{harness}' preset missing 'path'")
            if not isinstance(sha256, str) or not sha256:
                raise ValueError(f"harness '{harness}' preset missing 'sha256'")
        presets.append(
            HarnessPreset(
                harness=harness,
                version=version,
                path=path if isinstance(path, str) else "",
                sha256=sha256 if isinstance(sha256, str) else "",
                mode=mode,
            )
        )
    return tuple(presets)


def _artifacts_from_json(raw: object) -> dict[str, dict[str, str]]:
    """Validate the cc_switch_artifacts map: target -> {file, sha256}."""
    if not isinstance(raw, dict):
        raise ValueError("manifest 'cc_switch_artifacts' must be an object")
    out: dict[str, dict[str, str]] = {}
    for target, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"artifact for '{target}' must be an object")
        file: object = entry.get("file")
        sha256: object = entry.get("sha256")
        if not isinstance(file, str) or not file:
            raise ValueError(f"artifact for '{target}' missing 'file'")
        if not isinstance(sha256, str) or not sha256:
            raise ValueError(f"artifact for '{target}' missing 'sha256'")
        out[target] = {"file": file, "sha256": sha256}
    return out


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
