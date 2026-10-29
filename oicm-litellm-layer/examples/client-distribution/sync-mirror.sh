#!/usr/bin/env bash
# sync-mirror.sh — fetch cc-switch mirror content from the internet and push
# it to the gateway's admin API (schema 4). Run on a machine with internet.
#
# Usage:
#   export LITELLM_MASTER_KEY="<dev master key>"   # from the cluster Secret
#   export GATEWAY_URL="http://localhost:14001"    # dev gateway
#   ./sync-mirror.sh [--fetch-only] [--push-only]
#
# --fetch-only: download and stage artifacts only, no push
# --push-only:  push previously staged artifacts only, no fetch
# (default: fetch then push)
#
# Supply-chain rules: every download goes to a file and gets SHA-256 verified
# against the release's published checksums.txt before use. No curl|bash.
# Every tool is pinned by exact version + full URL.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STAGE_DIR="${SCRIPT_DIR}/.staging"

# Pinned cc-switch release — the ADEO fork, not upstream. Keep in sync with
# build-manifest.py. Artifact names embed the tag verbatim (no leading v on
# fork tags), and checksums.txt is what the fork release always publishes.
CC_SWITCH_VERSION="jya0-5.11.1"
GH_RELEASE="https://github.com/AITDEV00/cc-switch-cli/releases/download/${CC_SWITCH_VERSION}"

# Pinned jq release (official jqlang). Standalone static binaries; the
# installer embeds the same version + sha256 pins (it cannot read them from
# the manifest because parsing the manifest already needs jq — chicken and
# egg). sha256sum.txt is the release's published checksum sidecar. When
# bumping: update this version, the pins inside the installer template
# (litellm/proxy/client_distribution/generator.py), and re-push the template.
JQ_VERSION="1.8.2"
JQ_RELEASE="https://github.com/jqlang/jq/releases/download/jq-${JQ_VERSION}"
JQ_TARGETS=(
  "jq-linux-amd64"
  "jq-linux-arm64"
  "jq-macos-amd64"
  "jq-macos-arm64"
  "jq-windows-amd64.exe"
)

# cc-switch mirror targets -> archive extension
CC_SWITCH_TARGETS=(
  "linux-x64-musl:tar.gz"
  "linux-arm64-musl:tar.gz"
  "darwin-universal:tar.gz"
  "windows-x64:zip"
)

# Every harness carries a versioned preset file. Preset content is
# generated from the gateway's baked presets (single source of truth);
# bump the per-harness version in build-manifest.py's HARNESSES map and
# re-sync to change a shape. v2 adds per-model context windows + the
# default output cap; v3 (codex only) adds the modelCatalog block and drops
# the dead disable_response_storage key.
PROVIDER="adeo"
# Per-harness preset versions; MUST match build-manifest.py's HARNESSES map,
# which is the manifest's source of truth.
HARNESSES=(claude:2 codex:3 gemini:2 open-code:2 pi:2 hermes:2 open-claw:2)
# litellm checkout that has the baked presets (litellm/proxy/client_distribution)
LITELLM_ROOT="${LITELLM_ROOT:-$(cd "${SCRIPT_DIR}/../../.." && pwd)}"

fetch=1
push=1

for arg in "$@"; do
  case "$arg" in
    --fetch-only) push=0 ;;
    --push-only)  fetch=0 ;;
  esac
done

log()  { printf '[sync] %s\n' "$*"; }
die()  { printf '[sync] ERROR: %s\n' "$*" >&2; exit 1; }

download() {
  local url="$1" dest="$2"
  if [ -f "$dest" ]; then
    log "  cached: $(basename "$dest")"
    return 0
  fi
  log "  fetching: $(basename "$dest")"
  mkdir -p "$(dirname "$dest")"
  curl -fsSL --retry 3 --retry-delay 2 -o "$dest" "$url" \
    || die "download failed: $url"
}

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

# ── fetch ───────────────────────────────────────────────────────────────────

if [ "$fetch" -eq 1 ]; then
  log "=== Phase 1: fetch cc-switch ${CC_SWITCH_VERSION} checksums ==="
  CCS_DIR="${STAGE_DIR}/cc-switch/${CC_SWITCH_VERSION}"
  download "${GH_RELEASE}/checksums.txt" "${CCS_DIR}/checksums.txt"

  log "=== Phase 2: fetch + verify cc-switch archives ==="
  for spec in "${CC_SWITCH_TARGETS[@]}"; do
    target="${spec%%:*}"
    ext="${spec##*:}"
    artifact="cc-switch-cli-${CC_SWITCH_VERSION}-${target}.${ext}"
    download "${GH_RELEASE}/${artifact}" "${CCS_DIR}/${artifact}"

    # Verify against the release's published checksums.txt
    expected="$(grep "  ${artifact}\$" "${CCS_DIR}/checksums.txt" | awk '{print $1}')"
    [ -n "$expected" ] || die "no checksum for ${artifact} in checksums.txt"
    actual="$(sha256_of "${CCS_DIR}/${artifact}")"
    [ "$actual" = "$expected" ] \
      || die "SHA-256 mismatch for ${artifact}: expected $expected, got $actual"
    log "  verified: ${artifact}"
  done

  log "=== Phase 2b: fetch + verify jq ${JQ_VERSION} (static binaries) ==="
  JQ_DIR="${STAGE_DIR}/jq/${JQ_VERSION}"
  download "${JQ_RELEASE}/sha256sum.txt" "${JQ_DIR}/sha256sum.txt"
  for target in "${JQ_TARGETS[@]}"; do
    download "${JQ_RELEASE}/${target}" "${JQ_DIR}/${target}"
    expected="$(grep "  ${target}\$" "${JQ_DIR}/sha256sum.txt" | awk '{print $1}')"
    [ -n "$expected" ] || die "no checksum for ${target} in sha256sum.txt"
    actual="$(sha256_of "${JQ_DIR}/${target}")"
    [ "$actual" = "$expected" ] \
      || die "SHA-256 mismatch for ${target}: expected $expected, got $actual"
    log "  verified: ${target}"
  done

  log "=== Phase 3: stage harness presets (from the gateway's baked set) ==="
  # Presets are canonical per-harness skeletons with sentinels; the installer
  # fills the runtime values. Generated from BAKED_PRESETS so the store, the
  # baked fallback, and the docs can never drift apart.
  for spec in "${HARNESSES[@]}"; do
    harness="${spec%%:*}"
    preset_version="${spec##*:}"
    preset_dir="${STAGE_DIR}/providers/${PROVIDER}/${harness}"
    preset_file="${preset_dir}/v${preset_version}.json"
    mkdir -p "$preset_dir"
    ( cd "$LITELLM_ROOT" && .venv/bin/python - "$harness" > "$preset_file" ) <<'PYEOF' || die "failed to generate preset for $harness"
import sys
from litellm.proxy.client_distribution.generator import get_baked_preset
preset = get_baked_preset(sys.argv[1])
if preset is None:
    sys.exit(1)
sys.stdout.write(preset + "\n")
PYEOF
    log "  staged preset: ${preset_file}"
  done

  log "=== Phase 4: build manifest ==="
  python3 "${SCRIPT_DIR}/build-manifest.py" "$STAGE_DIR" \
    || die "manifest build failed"

  log "fetch complete: staged in ${STAGE_DIR}"
fi

# ── push ────────────────────────────────────────────────────────────────────

if [ "$push" -eq 1 ]; then
  [ -z "${LITELLM_MASTER_KEY:-}" ] && die "LITELLM_MASTER_KEY not set"
  [ -z "${GATEWAY_URL:-}" ] && die "GATEWAY_URL not set (e.g. http://localhost:14001)"

  BASE="${GATEWAY_URL%/}"
  KEY="$LITELLM_MASTER_KEY"

  log "=== Phase 5: push artifacts to ${BASE} ==="

  push_artifact() {
    local rel_path="$1" file_path="$2"
    local http_code
    http_code="$(curl -sS -o /dev/null -w '%{http_code}' \
      -X PUT "${BASE}/install/admin/v1/artifact/${rel_path}" \
      -H "Authorization: Bearer ${KEY}" \
      -H "Content-Type: application/octet-stream" \
      --data-binary "@${file_path}")"
    if [ "$http_code" -ge 400 ]; then
      die "push failed for ${rel_path}: HTTP ${http_code}"
    fi
    log "  pushed: ${rel_path} (HTTP ${http_code})"
  }

  # Push cc-switch binary artifacts under the versioned prefix
  for spec in "${CC_SWITCH_TARGETS[@]}"; do
    target="${spec%%:*}"
    ext="${spec##*:}"
    artifact="cc-switch-cli-${CC_SWITCH_VERSION}-${target}.${ext}"
    push_artifact "cc-switch/${CC_SWITCH_VERSION}/${artifact}" \
      "${STAGE_DIR}/cc-switch/${CC_SWITCH_VERSION}/${artifact}"
  done

  # Push jq static binaries under the versioned prefix
  for target in "${JQ_TARGETS[@]}"; do
    push_artifact "jq/${JQ_VERSION}/${target}" "${STAGE_DIR}/jq/${JQ_VERSION}/${target}"
  done

  # Push all harness presets at their pinned per-harness version
  for spec in "${HARNESSES[@]}"; do
    harness="${spec%%:*}"
    preset_version="${spec##*:}"
    rel="providers/${PROVIDER}/${harness}/v${preset_version}.json"
    push_artifact "$rel" "${STAGE_DIR}/${rel}"
  done

  # Push the install-script template if it has been modified locally.
  # The template is what /install renders from; iterating on it does not
  # require a gateway rebuild, only an admin PUT (PROXY_ADMIN-gated).
  TEMPLATE_LOCAL="${SCRIPT_DIR}/install.sh.tmpl"
  if [ -f "$TEMPLATE_LOCAL" ]; then
    push_artifact "templates/install.sh.tmpl" "$TEMPLATE_LOCAL"
  fi

  # Push manifest last (the commit point)
  log "=== Phase 6: publish manifest ==="
  http_code="$(curl -sS -o /dev/null -w '%{http_code}' \
    -X PUT "${BASE}/install/admin/v1/manifest" \
    -H "Authorization: Bearer ${KEY}" \
    -H "Content-Type: application/json" \
    --data-binary "@${STAGE_DIR}/manifest.json")"
  if [ "$http_code" -ge 400 ]; then
    die "manifest push failed: HTTP ${http_code}"
  fi
  log "manifest published (HTTP ${http_code})"

  # Verify
  log "=== Phase 7: verify ==="
  curl -fsSL "${BASE}/install/manifest.json" | python3 -m json.tool | head -8
  log "sync complete"
fi
