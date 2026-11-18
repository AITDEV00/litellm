#!/usr/bin/env bash
#
# Publish one source-agent release through the gateway into RustFS.
#
# Uses the same raw artifact channel as the cc-switch client: the operator holds
# only a per-environment Proxy Admin key, PUTs raw bytes to the gateway, and the
# gateway writes them to RustFS with its own store credentials. No store
# credential, no direct S3/mc endpoint, no base64 JSON bundle.
#
# Upload order matters: the immutable binary and its release.json go first, then
# the active pointer moves. An interrupted run therefore leaves the previous
# release serving instead of activating a half-uploaded one.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
layer=$(cd "$here/.." && pwd)

gateway=${GATEWAY:-}
case "$gateway" in
  dev) port=${DEV_PORT:-14010} ;;
  prod) port=${PROD_PORT:-4000} ;;
  *)
    echo "set GATEWAY=dev or GATEWAY=prod; refusing to guess a target" >&2
    exit 2
    ;;
esac

template_only=${TEMPLATE_ONLY:-0}

version=${VERSION:-}
if [[ "$template_only" != 1 ]]; then
  if [[ -z "$version" ]]; then
    echo "set VERSION to the release version to publish (e.g. VERSION=0.5.0)" >&2
    exit 2
  fi
  if [[ "$version" == .* || "$version" == */* || "$version" == *..* ]]; then
    echo "VERSION must be a plain path segment" >&2
    exit 2
  fi
fi

binary=${BINARY:-$layer/mcp-source-link/source-link}
release_json=${RELEASE_JSON:-}
template=${TEMPLATE:-$layer/templates/source-agent-install.sh.tmpl}
dry_run=${DRY_RUN:-0}

[[ -f "$template" ]] || { echo "missing artifact: $template" >&2; exit 2; }
command -v sh >/dev/null || { echo "sh is required to syntax-check the template" >&2; exit 2; }
sh -n "$template" || { echo "installer template fails 'sh -n': $template" >&2; exit 2; }

admin_key=${LITELLM_PROXY_ADMIN_KEY:-}
endpoint="http://127.0.0.1:${port}"

put_artifact() {
  local source=$1 key=$2
  local out
  out=$(curl -sS -w '\n%{http_code}' -X PUT \
    "$endpoint/install/admin/v1/artifact/$key" \
    -H "Authorization: Bearer $admin_key" \
    -H 'Content-Type: application/octet-stream' \
    --data-binary "@$source")
  local status=${out##*$'\n'}
  [[ "$status" == 200 ]] || { echo "upload of $key failed with HTTP $status" >&2; echo "${out%$'\n'*}" >&2; exit 1; }
  echo "  uploaded $key"
}

if [[ "$template_only" == 1 ]]; then
  # A template edit is its own release: upload it and stop. It is rendered per
  # request, so there is no version bump, pointer move or image rebuild, and no
  # built binary is required. Checking VERSION or the binary first would make this
  # mode unreachable without a full release build, which is the defect this branch
  # fixes.
  template_sha=$(sha256sum "$template" | cut -d' ' -f1)
  if [[ "$dry_run" == 1 ]]; then
    echo "dry run: would upload template ($template_sha) to the $gateway gateway; no pointer move"
    exit 0
  fi
  [[ -n "$admin_key" ]] || { echo "set LITELLM_PROXY_ADMIN_KEY to a Proxy Admin key for $gateway" >&2; exit 2; }
  put_artifact "$template" "templates/source-agent-install.sh.tmpl"
  echo "template updated ($template_sha); active release unchanged"
  exit 0
fi

for path in "$binary"; do
  [[ -f "$path" ]] || { echo "missing artifact: $path" >&2; exit 2; }
done

if ! file "$binary" | grep -q 'statically linked'; then
  echo "refusing to publish $binary: not a statically linked binary" >&2
  exit 2
fi

sha=$(sha256sum "$binary" | cut -d' ' -f1)
size=$(stat -c %s "$binary")
if (( size > 64*1024*1024 )); then
  echo "binary exceeds the 64 MiB release limit" >&2
  exit 2
fi

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
manifest="$scratch/release.json"
if [[ -n "$release_json" ]]; then
  cp "$release_json" "$manifest"
else
  printf '{"version":"%s","sha256":"%s","size":%s}\n' "$version" "$sha" "$size" >"$manifest"
fi
python_bin=${PYTHON:-$(command -v python3 || true)}
[[ -n "$python_bin" ]] || { echo "no python interpreter found; set PYTHON" >&2; exit 2; }
"$python_bin" - "$manifest" "$version" "$sha" <<'PY'
import json, sys
path, version, sha = sys.argv[1:4]
with open(path) as handle:
    data = json.load(handle)
if data.get("version") != version or data.get("sha256") != sha:
    raise SystemExit(f"release.json disagrees with the built binary: {data} vs {version}/{sha}")
if not isinstance(data.get("size", 0), int) or data.get("size", 0) < 0:
    raise SystemExit("release.json size must be a non-negative integer")
PY

if [[ "$dry_run" == 1 ]]; then
  echo "dry run: would publish source-agent $version ($sha, $size bytes) to the $gateway gateway"
  echo "dry run: binary -> source-agent/$version/adma"
  echo "dry run: metadata -> source-agent/$version/release.json"
  echo "dry run: template -> templates/source-agent-install.sh.tmpl"
  echo "dry run: pointer -> source-agent/manifest.json (last)"
  exit 0
fi

admin_key=${LITELLM_PROXY_ADMIN_KEY:-}
[[ -n "$admin_key" ]] || { echo "set LITELLM_PROXY_ADMIN_KEY to a Proxy Admin key for $gateway" >&2; exit 2; }

echo "publishing source-agent $version to $gateway"
put_artifact "$binary" "source-agent/$version/adma"
put_artifact "$manifest" "source-agent/$version/release.json"

template_sha=$(sha256sum "$template" | cut -d' ' -f1)
put_artifact "$template" "templates/source-agent-install.sh.tmpl"

state=$(curl -sS -H "Authorization: Bearer $admin_key" "$endpoint/install/admin/v1/source-agent/state")
current=$("$python_bin" -c 'import json,sys; print(json.load(sys.stdin)["token"])' <<<"$state")
activation=$(curl -sS -w '\n%{http_code}' -X PUT \
  "$endpoint/install/admin/v1/source-agent/activate" \
  -H "Authorization: Bearer $admin_key" \
  -H 'Content-Type: application/json' \
  --data-binary "$("$python_bin" -c 'import json,sys; print(json.dumps({"version": sys.argv[1], "sha256": sys.argv[2], "expected_current": sys.argv[3]}))' "$version" "$sha" "$current")")
act_status=${activation##*$'\n'}
[[ "$act_status" == 200 ]] || { echo "activation failed with HTTP $act_status" >&2; echo "${activation%$'\n'*}" >&2; exit 1; }

echo "activated $version ($sha); template $template_sha"
echo "verify: curl -s $endpoint/install/source-agent | head"
