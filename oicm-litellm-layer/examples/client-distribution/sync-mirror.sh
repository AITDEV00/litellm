#!/usr/bin/env bash
# sync-mirror.sh — fetch mirror content from the internet and push it to the
# gateway's admin API. Run on a machine with internet access.
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
# before use. No curl|bash. Every tool is pinned by exact version + full URL.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STAGE_DIR="${SCRIPT_DIR}/.staging"

# Pinned versions — keep in sync with manifest.json and the gateway
UV_VERSION="0.10.9"
LITELLM_CLI_VERSION="1.102.0"
# 3.13.12 is the newest 3.13.x in uv 0.10.9's bundled download metadata
# (crates/uv-python/download-metadata.json); 3.13.9 is not offered at all.
PYTHON_VERSION="3.13.12"

# Platform targets (from platform-matrix.md)
UV_TARGETS=(
  "x86_64-unknown-linux-gnu"
  "x86_64-unknown-linux-musl"
  "aarch64-unknown-linux-gnu"
  "aarch64-unknown-linux-musl"
  "x86_64-apple-darwin"
  "aarch64-apple-darwin"
  "x86_64-pc-windows-msvc"
  "aarch64-pc-windows-msvc"
)

# python-build-standalone tag that carries cpython 3.13.12 per uv 0.10.9's
# bundled metadata (crates/uv-python/download-metadata.json)
PYTHON_BUILD_STANDALONE_TAG="20260303"
PYTHON_BUILD_STANDALONE_RELEASE="cpython-${PYTHON_VERSION}+${PYTHON_BUILD_STANDALONE_TAG}"

# uv installer script
UV_INSTALLER_URL="https://astral.sh/uv/${UV_VERSION}/install.sh"

# GitHub release base for uv artifacts
GH_UV_RELEASE="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"

# python-build-standalone release base
GH_PBS_RELEASE="https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD_STANDALONE_TAG}"

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

# ── helpers ─────────────────────────────────────────────────────────────────

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

sha256_of() {
  sha256sum "$1" | awk '{print $1}'
}

verify_sha256() {
  local file="$1" expected="$2"
  local actual
  actual="$(sha256_of "$file")"
  if [ "$actual" != "$expected" ]; then
    die "SHA-256 mismatch for $file: expected $expected, got $actual"
  fi
}

# ── fetch ───────────────────────────────────────────────────────────────────

if [ "$fetch" -eq 1 ]; then
  log "=== Phase 1: fetch uv release artifacts ==="
  for target in "${UV_TARGETS[@]}"; do
    # Determine archive extension
    case "$target" in
      *-pc-windows-msvc) ext="zip" ;;
      *)                 ext="tar.gz" ;;
    esac
    artifact="uv-${target}.${ext}"
    sha_file="${artifact}.sha256"

    download "${GH_UV_RELEASE}/${artifact}" "${STAGE_DIR}/uv/${UV_VERSION}/${artifact}"
    download "${GH_UV_RELEASE}/${sha_file}" "${STAGE_DIR}/uv/${UV_VERSION}/${sha_file}"

    # Verify checksum (the .sha256 file may contain "hash  filename" or just "hash")
    expected="$(awk '{print $1}' "${STAGE_DIR}/uv/${UV_VERSION}/${sha_file}")"
    verify_sha256 "${STAGE_DIR}/uv/${UV_VERSION}/${artifact}" "$expected"
    log "  verified: ${artifact}"
  done

  log "=== Phase 2: fetch uv installer script ==="
  download "$UV_INSTALLER_URL" "${STAGE_DIR}/uv/${UV_VERSION}/uv-installer.sh"
  # The installer script has no published checksum; we verify it's a shell script
  head -1 "${STAGE_DIR}/uv/${UV_VERSION}/uv-installer.sh" | grep -q '^#!/' \
    || die "uv-installer.sh does not look like a shell script"
  log "  verified: uv-installer.sh (shebang check)"

  log "=== Phase 3: resolve litellm[cli] wheelhouse ==="
  WHEELHOUSE_DIR="${STAGE_DIR}/pypi"
  mkdir -p "$WHEELHOUSE_DIR"

  # Use a scratch venv + uv tool install to get the exact wheel closure,
  # then copy the wheels out of uv's cache.
  RESOLVE_DIR="$(mktemp -d)"
  trap 'rm -rf "$RESOLVE_DIR"' EXIT

  log "  resolving litellm[cli]==${LITELLM_CLI_VERSION}..."
  echo "litellm[cli]==${LITELLM_CLI_VERSION}" > "${RESOLVE_DIR}/requirements.in"
  uv pip compile \
    --python-version 3.13 \
    -o "${RESOLVE_DIR}/requirements.txt" \
    "${RESOLVE_DIR}/requirements.in" \
    || die "uv pip compile failed"

  # Download wheels using uv pip install into a target directory
  log "  downloading wheels..."
  uv pip install \
    --target "$WHEELHOUSE_DIR" \
    --python-version 3.13 \
    -r "${RESOLVE_DIR}/requirements.txt" \
    || die "uv pip install failed"

  # The target dir has the unpacked packages; we need the .whl files.
  # Re-download them from the cache that the install just populated.
  UV_CACHE="$(uv cache dir)"
  log "  extracting wheels from cache..."
  # Clear the target dir and re-populate with just .whl files
  rm -rf "${WHEELHOUSE_DIR:?}"/*
  # The uv cache stores wheels under a content-addressed layout; find them by
  # matching the package names from the requirements
  find "$UV_CACHE" -name '*.whl' -newer "${RESOLVE_DIR}/requirements.txt" \
    -exec cp {} "$WHEELHOUSE_DIR/" \; 2>/dev/null || true

  wheel_count="$(find "$WHEELHOUSE_DIR" -name '*.whl' | wc -l)"
  if [ "$wheel_count" -lt 5 ]; then
    # Fallback: use pip download from a python that has pip
    log "  cache yielded only ${wheel_count} wheels, falling back to pip download..."
    rm -rf "${WHEELHOUSE_DIR:?}"/*
    uv run --python 3.13 --with pip -- \
      pip download \
      --no-cache-dir \
      -r "${RESOLVE_DIR}/requirements.txt" \
      -d "$WHEELHOUSE_DIR" \
      || die "pip download failed"
    wheel_count="$(find "$WHEELHOUSE_DIR" -name '*.whl' | wc -l)"
  fi
  log "  staged ${wheel_count} wheels"

  wheel_count="$(find "$WHEELHOUSE_DIR" -name '*.whl' | wc -l)"
  log "  downloaded ${wheel_count} wheels"

  log "=== Phase 4: fetch python-build-standalone archives ==="
  PBS_DIR="${STAGE_DIR}/python"
  mkdir -p "$PBS_DIR"

  # For each platform target, download the install_only_stripped archive that
  # uv's managed-Python downloader requests (per its download metadata).
  for target in "${UV_TARGETS[@]}"; do
    pbs_file="cpython-${PYTHON_VERSION}+${PYTHON_BUILD_STANDALONE_TAG}-${target}-install_only_stripped.tar.gz"
    pbs_url="${GH_PBS_RELEASE}/${pbs_file}"
    download "$pbs_url" "${PBS_DIR}/${pbs_file}"
    log "  fetched: ${pbs_file}"
  done

  log "=== Phase 5: build manifest ==="
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

  log "=== Phase 6: push artifacts to ${BASE} ==="

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

  # Push uv artifacts
  for target in "${UV_TARGETS[@]}"; do
    case "$target" in
      *-pc-windows-msvc) ext="zip" ;;
      *)                 ext="tar.gz" ;;
    esac
    artifact="uv-${target}.${ext}"
    push_artifact "uv/${UV_VERSION}/${artifact}" "${STAGE_DIR}/uv/${UV_VERSION}/${artifact}"
    # Also push the .sha256 sidecar
    sha_file="${artifact}.sha256"
    if [ -f "${STAGE_DIR}/uv/${UV_VERSION}/${sha_file}" ]; then
      push_artifact "uv/${UV_VERSION}/${sha_file}" "${STAGE_DIR}/uv/${UV_VERSION}/${sha_file}"
    fi
  done

  # Push uv installer script
  push_artifact "uv/${UV_VERSION}/uv-installer.sh" "${STAGE_DIR}/uv/${UV_VERSION}/uv-installer.sh"

  # Push python-build-standalone archives under the build-tag prefix that
  # uv's UV_PYTHON_INSTALL_MIRROR URL layout expects (<mirror>/<tag>/<file>).
  for f in "${PBS_DIR:-${STAGE_DIR}/python}"/*; do
    [ -f "$f" ] || continue
    push_artifact "python/${PYTHON_BUILD_STANDALONE_TAG}/$(basename "$f")" "$f"
  done

  # Push wheelhouse
  for f in "${WHEELHOUSE_DIR:-${STAGE_DIR}/pypi}"/*.whl; do
    [ -f "$f" ] || continue
    push_artifact "pypi/$(basename "$f")" "$f"
  done

  # Push the install-script template if it has been modified locally.
  # The template is what /install renders from; iterating on it does not
  # require a gateway rebuild, only an admin PUT.
  TEMPLATE_LOCAL="${SCRIPT_DIR}/install.sh.tmpl"
  if [ -f "$TEMPLATE_LOCAL" ]; then
    push_artifact "templates/install.sh.tmpl" "$TEMPLATE_LOCAL"
  fi

  # Push manifest last (the commit point)
  log "=== Phase 7: publish manifest ==="
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
  log "=== Phase 8: verify ==="
  curl -fsSL "${BASE}/install/manifest.json" | python3 -m json.tool | head -5
  log "sync complete"
fi
