"""Pure install-script generator for /install.

Takes the mirror manifest and the gateway's public base URL, returns the
POSIX shell script text. No FastAPI imports; this is a pure function so it
can be unit-tested without an app instance.

The template is a Jinja2 document rendered with a structured context, so the
script can grow conditionals and loops without the renderer changing. It is
a **generic orchestrator** (see docs/client-distribution/installer-script.md):

1. fetch the manifest, then prompt immediately: the gateway key (visible
   input, validated against ``/api/v1/models``, key-scoped with real context
   windows) and the harness multi-select (the list is baked at render time
   so the menu needs no jq),
2. only after the user has answered, do the installing: detect the
   platform, bootstrap jq from the gateway mirror, install cc-switch
   (both sha256-verified against the manifest),
3. per chosen harness: fetch the manifest-pinned preset, verify its sha256,
   compose the final cc-switch config client-side with jq (never string
   substitution) from runtime values, per-model context windows, and the
   manifest's default output cap; upsert the provider, and activate it.

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

# Manifest default for the per-request output cap written into harness
# configs that have a real output knob (claude env key, opencode limit.output).
_DEFAULT_MAX_OUTPUT_TOKENS: Final = 65536

_TEMPLATE: Final = r"""#!/usr/bin/env bash
# ADEO coding-agent setup (cc-switch)
# Served by {{ base_url }} — installs cc-switch and points your coding
# harnesses at the gateway. No external egress required.
set -eu

CC_SWITCH_VERSION="{{ cc_switch_version }}"
BASE_URL="${ADEO_LITELLM_BASE_URL:-{{ base_url }}}"
PROVIDER_NAME="ADEO LiteLLM"
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
printf "  ${BOLD}ADEO coding-agent setup${RESET}  ${GREY}cc-switch -> your gateway${RESET}\n\n"
info "Gateway: $BASE_URL"
info "cc-switch: $CC_SWITCH_VERSION"

command -v curl >/dev/null 2>&1 || die "curl is required but not installed."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ── fetch manifest (the integrity source of truth) ────────────────────────
# TLS failures get a named fix: curl exit 60 (cert rejected) and 77 (no CA
# bundle on the system) both mean the OS lacks trusted roots. That cannot be
# bootstrapped over TLS itself (circular), so name the package to install.
# NOTE: an `if ! curl ...` condition would invert $?, so capture via `|| RC=$?`.
RC=0
curl -fsSL "$BASE_URL/install/manifest.json" -o "$TMP/manifest.json" || RC=$?
if [ "$RC" -ne 0 ]; then
  case "$RC" in
    60|77) die "TLS failed (curl exit $RC): this system has no trusted CA certificates. Install them, e.g. 'apt-get install ca-certificates' (Ubuntu/Debian) or 'apk add ca-certificates' (Alpine), then re-run" ;;
    *)     die "Failed to fetch manifest from $BASE_URL/install/manifest.json (curl exit $RC)" ;;
  esac
fi

# ── 0. interactive input ──────────────────────────────────────────────────
# When the script runs as `curl ... | sh`, stdin IS the script itself, so a
# plain `read` would consume the next script line as the answer instead of
# waiting for the keyboard. All prompts therefore read from /dev/tty.
# The probe must be an `exec` open, never a `{ : < /dev/tty; }` redirect:
# a redirect on a piped stdin shell consumes the next script line from the
# pipe as input. In a headless context /dev/tty exists as a node but cannot
# be opened (ENXIO), and only the open fails.
TTY_INPUT="/dev/tty"
if ! (exec 3< "$TTY_INPUT") 2>/dev/null; then
  die "this installer asks for your gateway key interactively; run it from a terminal (or: curl -fsSL URL -o install.sh && ./install.sh)"
fi

# ── 1. prompt for the gateway key (visible input) ─────────────────────────
echo ""
header "Gateway API key"
printf "  Paste your ADEO LiteLLM virtual key, then Enter: " > "$TTY_INPUT"
if ! IFS= read -r API_KEY < "$TTY_INPUT"; then echo "" > "$TTY_INPUT"; die "no key entered"; fi
echo "" > "$TTY_INPUT"
[ -n "$API_KEY" ] || die "no key entered"

curl -fsSL -H "Authorization: Bearer $API_KEY" "$BASE_URL/api/v1/models?limit=2000" -o "$TMP/models.json" \
  || die "that key was rejected by $BASE_URL; check it with your platform admin"
info "key accepted"

# ── 2. harness selection (list baked at render time, no jq needed) ───────
# The harness names are rendered from the manifest into this script, so the
# menu can appear before jq is bootstrapped. Adding a harness stays a store
# sync, not a script change: a new manifest re-renders the list.
HARNESSES="{% for h in harnesses | sort(attribute='harness') %}{{ h.harness }} {% endfor %}"
[ -n "$HARNESSES" ] || die "manifest declares no harnesses"
echo ""
header "Which harnesses should route through the gateway?"
i=1
for h in $HARNESSES; do printf "    %d) %s\n" "$i" "$h"; i=$((i+1)); done
printf "  Enter numbers separated by spaces (e.g. 1 3): " > "$TTY_INPUT"
if ! IFS= read -r PICKS < "$TTY_INPUT"; then echo "" > "$TTY_INPUT"; die "nothing selected"; fi
[ -n "$PICKS" ] || die "nothing selected"

CHOSEN=""
for n in $PICKS; do
  h="$(echo "$HARNESSES" | cut -d' ' -f"$n" 2>/dev/null || true)"
  [ -n "$h" ] || die "invalid selection: $n"
  CHOSEN="$CHOSEN $h"
done

# ── 3. install: platform, jq, cc-switch ────────────────────────────────────
# Everything downloadable is deferred to here so the user answers all
# prompts before the first byte is fetched beyond the manifest.

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

# ── jq bootstrap (pinned in the script: parsing the manifest needs jq) ────
# The pins cannot live in the manifest — reading them would already require
# jq. jq 1.8.2 official binaries are standalone static executables, mirrored
# in the gateway store under jq/<version>/, same flow as cc-switch.
JQ_VERSION="1.8.2"
JQ_FILE=""
JQ_SHA256=""
case "$OS-$ARCH" in
  Linux-x86_64)   JQ_FILE="jq-linux-amd64";     JQ_SHA256="b1c22172dd303f3be49e935aa56aa48a8b7a46e0bc838b4997d3bb451495870f" ;;
  Linux-aarch64)  JQ_FILE="jq-linux-arm64";     JQ_SHA256="8b85c817833814ddca00a144c33705546355afccf0cf39b188f3cdb48b852309" ;;
  Darwin-x86_64)  JQ_FILE="jq-macos-amd64";    JQ_SHA256="e94b266e3c26690550006abe63152b782280f4e14374accdf04cbde844f00bc0" ;;
  Darwin-arm64)   JQ_FILE="jq-macos-arm64";    JQ_SHA256="2d75340ba57a4b4b4c8708a21c2dc8e958a48aaa8bba13b27f77f6e4c0eca07e" ;;
  Windows-x86_64) JQ_FILE="jq-windows-amd64.exe"; JQ_SHA256="a6fc67fedaf9128a3309a1e2ebb8b986aeccf70122ee46d2cb4849e423f0c627" ;;
  *)              ;;
esac

JQ_BIN=""
if command -v jq >/dev/null 2>&1; then JQ_BIN="jq"; fi
if [ -z "$JQ_BIN" ] && [ -x "$INSTALL_DIR/jq" ]; then JQ_BIN="$INSTALL_DIR/jq"; fi
if [ -z "$JQ_BIN" ] && [ -x "$INSTALL_DIR/jq.exe" ]; then JQ_BIN="$INSTALL_DIR/jq.exe"; fi

if [ -n "$JQ_BIN" ]; then
  info "jq: $JQ_BIN ($("$JQ_BIN" --version 2>/dev/null || echo 'unknown version'))"
elif [ -n "$JQ_FILE" ]; then
  header "Installing jq $JQ_VERSION..."
  curl -fsSL "$BASE_URL/install/jq/$JQ_VERSION/$JQ_FILE" -o "$TMP/jq-download" \
    || die "download failed: $BASE_URL/install/jq/$JQ_VERSION/$JQ_FILE"
  echo "$JQ_SHA256  $TMP/jq-download" | sha256sum -c - >/dev/null \
    || die "checksum mismatch for jq — the store may be corrupt; contact the platform team"
  mkdir -p "$INSTALL_DIR"
  if [ "$OS" = "Windows" ]; then
    mv "$TMP/jq-download" "$INSTALL_DIR/jq.exe"
    chmod +x "$INSTALL_DIR/jq.exe"
    JQ_BIN="$INSTALL_DIR/jq.exe"
  else
    mv "$TMP/jq-download" "$INSTALL_DIR/jq"
    chmod +x "$INSTALL_DIR/jq"
    JQ_BIN="$INSTALL_DIR/jq"
  fi
  info "jq installed at: $JQ_BIN"
else
  die "jq is required but not installed, and the gateway stores no jq build for $OS ($ARCH). Install it with your package manager, e.g. 'apt-get install jq', then re-run"
fi
"$JQ_BIN" --version >/dev/null 2>&1 || die "the jq binary at $JQ_BIN does not run on this system"
MODEL_COUNT="$("$JQ_BIN" -r '.data | length' "$TMP/models.json" 2>/dev/null || echo 0)"
[ "$MODEL_COUNT" -gt 0 ] || die "the key works but can call no models; ask your admin for model access"
info "key OK — $MODEL_COUNT model(s) visible to this key"

# ── 3b. install cc-switch (skip if version matches) ─────────────────────
CCS_BIN=""
for candidate in cc-switch "$INSTALL_DIR/cc-switch"; do
  if command -v "$candidate" >/dev/null 2>&1; then CCS_BIN="$(command -v "$candidate")"; break
  elif [ -x "$candidate" ]; then CCS_BIN="$candidate"; break; fi
done

if [ -n "$CCS_BIN" ] && [ "$("$CCS_BIN" --version 2>/dev/null | awk '{print $2}')" = "$CC_SWITCH_VERSION" ]; then
  info "cc-switch $CC_SWITCH_VERSION already installed, skipping"
else
  header "Installing cc-switch $CC_SWITCH_VERSION..."
  FILE="$("$JQ_BIN" -r --arg t "$TARGET" '.cc_switch_artifacts[$t].file' "$TMP/manifest.json")"
  WANT="$("$JQ_BIN" -r --arg t "$TARGET" '.cc_switch_artifacts[$t].sha256' "$TMP/manifest.json")"
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

# ── 4. per-harness provider reconcile (backup + add + use) ────────────────
# Generic orchestrator: the per-harness cc-switch JSON shape lives in the
# versioned preset the manifest pins, not in this script. The script fetches
# the preset, verifies it against the manifest sha256, then composes the
# final config client-side with jq from runtime values (base URL, API key,
# accessible models with their context windows, the manifest's output cap) —
# never string substitution.
umask 077
MAX_OUTPUT_TOKENS="{{ max_output_tokens }}"
# Text-generation models this key can call, each with its real context
# window from /api/v1/models. Utility families (speech, OCR, embeddings,
# image generation, document processing) are excluded by name because the
# payload carries no capability signal; UUID-named models likewise.
MODELS_JSON="$("$JQ_BIN" -c '
  [.data[]
   | select((.id | test("embed|tts|stt|asr|ocr|transcri|voice|speech|image|rerank|layout|whisper"; "i")) | not)
   | select((.id | test("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-")) | not)
   | {id: .id, context: (if (.context_length // 0) > 0 then .context_length else null end)}]
  | .[0:20]' "$TMP/models.json" 2>/dev/null || echo '[]')"
[ "$(printf '%s' "$MODELS_JSON" | "$JQ_BIN" 'length' 2>/dev/null || echo 0)" -gt 0 ] \
  || die "no text-generation models are accessible to this key; ask your admin"
DEFAULT_MODEL="$(printf '%s' "$MODELS_JSON" | "$JQ_BIN" -r '.[0].id')"
DEFAULT_CTX_JSON="$(printf '%s' "$MODELS_JSON" | "$JQ_BIN" -c '.[0].context | if . != null then tostring else null end')"
DEFAULT_CTX_STR="$(printf '%s' "$MODELS_JSON" | "$JQ_BIN" -r '.[0].context // "" | tostring')"
info "default model: $DEFAULT_MODEL (context window: ${DEFAULT_CTX_STR:-unknown})"
info "max output tokens per request: $MAX_OUTPUT_TOKENS (gateway default)"

configure_harness() {
  local harness="$1" preset_path="$2" preset_sha="$3"
  header "Configuring $harness..."
  "$CCS_BIN" config backup >/dev/null 2>&1 || true

  # Fetch the pinned preset and verify it against the manifest.
  curl -fsSL "$BASE_URL/install/$preset_path" -o "$TMP/preset.json" \
    || die "failed to fetch preset for $harness: $BASE_URL/install/$preset_path"
  echo "$preset_sha  $TMP/preset.json" | sha256sum -c - >/dev/null \
    || die "preset checksum mismatch for $harness — the store may be corrupt"

  # One generic compose: substitute sentinels anywhere in the preset. The
  # preset alone owns the harness's config shape; this script knows none of
  # it. Each preset embeds the models placeholder whose shape it needs:
  #   ["__MODELS_ARRAY_CTX__"]        -> [{id,name,contextWindow?}]   (pi, open-claw)
  #   ["__MODELS_ARRAY_CTX_SNAKE__"]  -> [{id,name,context_length?}]       (hermes)
  #   {"__MODELS_OBJECT_LIMIT__": null} -> {id:{name,limit:{context?,output}}} (open-code)
  # A model with no reported context simply omits the context key.
  MODELS_ARRAY_CTX="$(printf '%s' "$MODELS_JSON" | "$JQ_BIN" -c 'map({id: .id, name: .id} + (if .context then {contextWindow: .context} else {} end))')"
  MODELS_ARRAY_CTX_SNAKE="$(printf '%s' "$MODELS_JSON" | "$JQ_BIN" -c 'map({id: .id, name: .id} + (if .context then {context_length: .context} else {} end))')"
  MODELS_OBJECT_LIMIT="$(printf '%s' "$MODELS_JSON" | "$JQ_BIN" -c --argjson maxout "$MAX_OUTPUT_TOKENS" \
    'map({key: .id, value: {name: .id, limit: ((if .context then {context: .context} else {} end) + {output: $maxout})}}) | from_entries')"
  "$JQ_BIN" \
    --arg url "$BASE_URL" \
    --arg url_v1 "$BASE_URL/v1" \
    --arg key "$API_KEY" \
    --arg model "$DEFAULT_MODEL" \
    --arg ctx "$DEFAULT_CTX_STR" \
    --argjson max_output "$MAX_OUTPUT_TOKENS" \
    --argjson default_ctx "$DEFAULT_CTX_JSON" \
    --argjson models_array_ctx "$MODELS_ARRAY_CTX" \
    --argjson models_array_ctx_snake "$MODELS_ARRAY_CTX_SNAKE" \
    --argjson models_object_limit "$MODELS_OBJECT_LIMIT" \
    'walk(
       if type == "string" and . == "__DEFAULT_MODEL_CONTEXT__" then $default_ctx
       elif type == "string" then
         (if $ctx == ""
          then gsub("model_context_window = __DEFAULT_MODEL_CONTEXT__\n"; "")
          else gsub("__DEFAULT_MODEL_CONTEXT__"; $ctx) end)
         | gsub("__BASE_URL__"; $url)
         | gsub("__BASE_URL_V1__"; $url_v1)
         | gsub("__API_KEY__"; $key)
         | gsub("__DEFAULT_MODEL__"; $model)
         | gsub("__MAX_OUTPUT_TOKENS__"; ($max_output | tostring))
       elif type == "array" and . == ["__MODELS_ARRAY_CTX__"] then $models_array_ctx
       elif type == "array" and . == ["__MODELS_ARRAY_CTX_SNAKE__"] then $models_array_ctx_snake
       elif type == "object" and . == {"__MODELS_OBJECT_LIMIT__": null} then $models_object_limit
       else . end)
     | walk(if type == "object" then with_entries(select(.value != null)) else . end)' \
    "$TMP/preset.json" > "$TMP/provider.json" \
    || die "failed to compose config for $harness"

  # One stable id per gateway key: the key's last 4 chars (lowercased,
  # dash-stripped to satisfy cc-switch's id charset). Re-running the script
  # refreshes the same provider in place via --overwrite (our cc-switch fork),
  # so a rotated key or changed model list replaces the stored config instead
  # of piling up revisions.
  PROVIDER_ID="adeo-litellm-$(printf '%s' "$API_KEY" | tail -c 4 | tr '[:upper:]' '[:lower:]' | tr -d '-')"
  "$CCS_BIN" provider add --app "$harness" --id "$PROVIDER_ID" --name "$PROVIDER_NAME" \
      --config-file "$TMP/provider.json" --overwrite >/dev/null 2>&1 \
    || die "provider add --overwrite failed for $harness"

  # use is the gate: it points the harness at our provider and writes its
  # native config file. This is what must succeed.
  "$CCS_BIN" use "$PROVIDER_ID" --app "$harness" </dev/null \
    || die "failed to activate $PROVIDER_ID for $harness"
  rm -f "$TMP/preset.json" "$TMP/provider.json"
  success "$harness configured"
}

for HARNESS in $CHOSEN; do
  echo ""
  PPATH="$("$JQ_BIN" -r --arg h "$HARNESS" '.harnesses[$h].path // empty' "$TMP/manifest.json")"
  PSHA="$("$JQ_BIN" -r --arg h "$HARNESS" '.harnesses[$h].sha256 // empty' "$TMP/manifest.json")"
  [ -n "$PPATH" ] || die "manifest pins no preset for $HARNESS"
  # Detach stdin: curl/jq/cc-switch in the configure step would otherwise
  # consume the piped harness picks and stall waiting for input.
  configure_harness "$HARNESS" "$PPATH" "$PSHA" </dev/null
done

# ── done ───────────────────────────────────────────────────────────────────
echo ""
"$CCS_BIN" env check 2>/dev/null || true
echo ""
success "Done. Restart each configured harness to pick up the gateway."
info "Re-run this script any time to re-sync models:  curl -fsSL $BASE_URL/install | sh"
info "Update cc-switch only via this script, not 'cc-switch update'."
if ! command -v cc-switch >/dev/null 2>&1 && [ -x "$INSTALL_DIR/cc-switch" ]; then
  echo ""
  info "cc-switch is installed at $INSTALL_DIR/cc-switch but that directory is not on PATH."
  info "Fix it for this shell:"
  info "    export PATH=\"$INSTALL_DIR:\$PATH\""
  info "and for future shells, add that line to your ~/.profile or ~/.bashrc."
  info "(jq, if bootstrapped, lives in the same directory.)"
fi
echo ""
"""


# Baked-in fallback presets for every harness, keyed by cc-switch --app id.
# Each is the canonical settings_config shape cc-switch v5.11.1 itself stores
# for that harness (verified against the binary), with sentinels where runtime
# values go. The store-backed versioned preset
# (providers/adeo/<harness>/v<N>.json) overrides these when the manifest pins
# one — these exist so a harness works even before its first preset sync.
#
# Sentinels: __BASE_URL__ / __BASE_URL_V1__ (gateway root / +v1),
# __API_KEY__, __DEFAULT_MODEL__ (first text-generation model the key can
# call), __DEFAULT_MODEL_CONTEXT__ (its context window; the compose drops the
# field entirely when the gateway reports none), __MAX_OUTPUT_TOKENS__ (the
# manifest's default per-request output cap), and the whole-value models
# placeholders ["__MODELS_ARRAY_CTX__"] -> [{id,name,contextWindow?}...],
# ["__MODELS_ARRAY_CTX_SNAKE__"] -> [{id,name,context_length?}...], and
# {"__MODELS_OBJECT_LIMIT__": null} -> {id:{name,limit:{context?,output}}...}.
#
# Context/output land only where the harness has a real, verified knob:
# claude env keys, codex TOML key, opencode limit block, pi/open-claw
# contextWindow, hermes context_length. gemini's writer drops any env key
# outside its hardcoded allow-list, so its preset carries no limit fields.
BAKED_PRESETS: Final[dict[str, str]] = {
    # claude: env block; cc-switch's own field-mode add also mirrors the
    # default model into the three role-model slots, so we do the same.
    # Both CLAUDE_CODE_* keys are cc-switch-managed exclusive env vars
    # written on `use`: MAX_CONTEXT_TOKENS is the model's real context
    # window, MAX_OUTPUT_TOKENS the per-request output cap.
    "claude": json.dumps(
        {
            "env": {
                "ANTHROPIC_AUTH_TOKEN": "__API_KEY__",
                "ANTHROPIC_BASE_URL": "__BASE_URL__",
                "ANTHROPIC_MODEL": "__DEFAULT_MODEL__",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "__DEFAULT_MODEL__",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "__DEFAULT_MODEL__",
                "ANTHROPIC_DEFAULT_OPUS_MODEL": "__DEFAULT_MODEL__",
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "__DEFAULT_MODEL_CONTEXT__",
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "__MAX_OUTPUT_TOKENS__",
            }
        },
        indent=2,
    ),
    # codex: TOML config string + auth block (cc-switch's own stored shape).
    # model_context_window is a provider-exclusive TOML key cc-switch
    # respects (its own fallback when the key is absent is 128000); Codex
    # itself has no max-output config knob, so only the context is set.
    "codex": json.dumps(
        {
            "config": (
                "model_provider = \"adeo-litellm\"\n"
                "model = \"__DEFAULT_MODEL__\"\n"
                "model_reasoning_effort = \"high\"\n"
                "model_context_window = __DEFAULT_MODEL_CONTEXT__\n"
                "disable_response_storage = true\n"
                "\n"
                "[model_providers.adeo-litellm]\n"
                "name = \"ADEO LiteLLM\"\n"
                "base_url = \"__BASE_URL__\"\n"
                "wire_api = \"responses\"\n"
                "requires_openai_auth = true"
            ),
            "auth": {"OPENAI_API_KEY": "__API_KEY__"},
        },
        indent=2,
    ),
    # gemini: env block with GEMINI_* keys (cc-switch's own stored shape).
    "gemini": json.dumps(
        {
            "env": {
                "GEMINI_API_KEY": "__API_KEY__",
                "GOOGLE_GEMINI_BASE_URL": "__BASE_URL__",
                "GEMINI_MODEL": "__DEFAULT_MODEL__",
            }
        },
        indent=2,
    ),
    # open-code: provider entry for opencode.json; models as
    # {id:{name,limit:{context,output}}} — opencode's own per-model limit
    # block, context from the gateway, output from the manifest cap.
    "open-code": json.dumps(
        {
            "npm": "@ai-sdk/openai-compatible",
            "name": "ADEO LiteLLM",
            "options": {"baseURL": "__BASE_URL_V1__", "apiKey": "__API_KEY__"},
            "models": {"__MODELS_OBJECT_LIMIT__": None},
        },
        indent=2,
    ),
    # hermes: snake_case keys; models as [{id,name,context_length}] array
    # (api_mode chat_completions = OpenAI-compatible; context_length is
    # hermes's own per-model key).
    "hermes": json.dumps(
        {
            "api_mode": "chat_completions",
            "base_url": "__BASE_URL_V1__",
            "api_key": "__API_KEY__",
            "models": ["__MODELS_ARRAY_CTX_SNAKE__"],
        },
        indent=2,
    ),
    # open-claw: flat camelCase; models as [{id,name,contextWindow}] array,
    # >= 1 required (contextWindow is openclaw's own per-model key).
    "open-claw": json.dumps(
        {
            "baseUrl": "__BASE_URL_V1__",
            "apiKey": "__API_KEY__",
            "models": ["__MODELS_ARRAY_CTX__"],
        },
        indent=2,
    ),
    # pi: flat keys; models as [{id,name,contextWindow}] array (pi reads the
    # provider-level flat baseUrl; contextWindow is pi's own per-model key).
    "pi": json.dumps(
        {
            "name": "ADEO LiteLLM",
            "apiKey": "__API_KEY__",
            "baseUrl": "__BASE_URL_V1__",
            "models": ["__MODELS_ARRAY_CTX__"],
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
    """One harness's versioned cc-switch configuration slice (schema 5).

    The manifest pins each harness to an immutable, store-backed preset so a
    harness's cc-switch settings_config shape can be revised without a
    gateway rebuild. Every harness has a preset; the shape it encodes is the
    harness's own business, so no mode split is needed.
    """

    harness: str  # cc-switch --app id, e.g. "open-code"
    version: int
    path: str  # store key, e.g. "providers/adeo/open-code/v3.json"
    sha256: str


@dataclass(frozen=True, slots=True)
class MirrorManifest:
    """The fields from manifest.json the generator needs (schema 5)."""

    cc_switch_version: str
    cc_switch_artifacts: dict[str, dict[str, str]]
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS
    harnesses: tuple[HarnessPreset, ...] = field(default=())
    platforms: tuple[PlatformTarget, ...] = field(default=())
    def to_context(self, base_url: str) -> dict[str, object]:
        """Build the Jinja2 render context for this manifest + gateway."""
        return {
            "base_url": base_url,
            "cc_switch_version": self.cc_switch_version,
            "cc_switch_artifacts": self.cc_switch_artifacts,
            "max_output_tokens": self.max_output_tokens,
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
    """Return the baked-in fallback preset for a harness, or None."""
    return BAKED_PRESETS.get(harness)


def manifest_from_json(raw: str) -> MirrorManifest:
    """Parse a manifest.json string into a MirrorManifest (schema 5).

    Raises ``ValueError`` on missing or malformed fields. ``platforms`` is a
    list of ``{"target": ..., "archive_ext": ...}`` objects; when omitted the
    manifest carries no platform metadata and the template renders an empty
    platform set. ``harnesses`` maps a cc-switch ``--app`` id to a pinned
    preset ``{"version", "path", "sha256"}`` (schema 5).
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

    max_output_tokens: object = data.get("max_output_tokens", _DEFAULT_MAX_OUTPUT_TOKENS)
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int):
        raise ValueError("manifest 'max_output_tokens' must be an integer")
    if max_output_tokens <= 0:
        raise ValueError("manifest 'max_output_tokens' must be positive")

    return MirrorManifest(
        cc_switch_version=cc_switch_version,
        cc_switch_artifacts=_artifacts_from_json(cc_switch_artifacts),
        max_output_tokens=max_output_tokens,
        harnesses=_harnesses_from_json(data.get("harnesses")),
        platforms=_platforms_from_json(data.get("platforms")),
    )


def _harnesses_from_json(raw: object) -> tuple[HarnessPreset, ...]:
    """Parse the optional ``harnesses`` map into HarnessPreset tuples."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError("manifest 'harnesses' must be an object")
    return tuple(
        _parse_harness_preset(harness, entry) for harness, entry in raw.items()
    )


def _parse_harness_preset(harness: str, entry: object) -> HarnessPreset:
    """Validate one harness preset entry and return the typed dataclass."""
    if not isinstance(entry, dict):
        raise ValueError(f"harness '{harness}' preset must be an object")
    version: object = entry.get("version")
    path: object = entry.get("path")
    sha256: object = entry.get("sha256")
    if not isinstance(version, int):
        raise ValueError(f"harness '{harness}' preset missing int 'version'")
    if not isinstance(path, str) or not path:
        raise ValueError(f"harness '{harness}' preset missing 'path'")
    if not isinstance(sha256, str) or not sha256:
        raise ValueError(f"harness '{harness}' preset missing 'sha256'")
    return HarnessPreset(harness=harness, version=version, path=path, sha256=sha256)


def _artifacts_from_json(raw: object) -> dict[str, dict[str, str]]:
    """Validate the cc_switch_artifacts map: target -> {file, sha256}."""
    if not isinstance(raw, dict):
        raise ValueError("manifest 'cc_switch_artifacts' must be an object")
    return {
        target: _parse_artifact(target, entry) for target, entry in raw.items()
    }


def _parse_artifact(target: str, entry: object) -> dict[str, str]:
    """Validate one artifact entry and return the normalized dict."""
    if not isinstance(entry, dict):
        raise ValueError(f"artifact for '{target}' must be an object")
    file: object = entry.get("file")
    sha256: object = entry.get("sha256")
    if not isinstance(file, str) or not file:
        raise ValueError(f"artifact for '{target}' missing 'file'")
    if not isinstance(sha256, str) or not sha256:
        raise ValueError(f"artifact for '{target}' missing 'sha256'")
    return {"file": file, "sha256": sha256}


def _platforms_from_json(raw: object) -> tuple[PlatformTarget, ...]:
    """Parse the optional ``platforms`` list into PlatformTarget tuples."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("manifest 'platforms' must be a list")
    return tuple(_parse_platform(entry) for entry in raw)


def _parse_platform(entry: object) -> PlatformTarget:
    """Validate one platform entry and return the typed dataclass."""
    if not isinstance(entry, dict):
        raise ValueError("manifest 'platforms' entries must be objects")
    target: object = entry.get("target")
    archive_ext: object = entry.get("archive_ext")
    if not isinstance(target, str) or not target:
        raise ValueError("platform entry missing 'target'")
    if not isinstance(archive_ext, str) or not archive_ext:
        raise ValueError(f"platform '{target}' missing 'archive_ext'")
    return PlatformTarget(target=target, archive_ext=archive_ext)
