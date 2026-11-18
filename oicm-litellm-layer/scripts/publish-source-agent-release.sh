#!/usr/bin/env bash
#
# Publish one source-agent release: the statically built binary, its release.json,
# and the checked-in install script, as the single JSON body the admin route
# expects. Nothing here builds or deploys an image, and nothing touches the
# gateway Deployment; it only uploads immutable assets and moves the release
# pointer last.
#
# The gateway is selected explicitly. There is no default that silently means
# prod: an unset or unknown GATEWAY is refused before any read of the artifacts.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
module=$(cd "$here/.." && pwd)

gateway=${GATEWAY:-}
case "$gateway" in
    dev) port=${DEV_PORT:-14010} ;;
    prod) port=${PROD_PORT:-4000} ;;
    *)
        echo "set GATEWAY=dev or GATEWAY=prod; refusing to guess a target" >&2
        exit 2
        ;;
esac

version=${VERSION:-}
if [[ -z "$version" ]]; then
    echo "set VERSION to the release version to publish (e.g. VERSION=v1.2.3)" >&2
    exit 2
fi

binary=${BINARY:-$module/source-link}
install_script=${INSTALL_SCRIPT:-$module/mcp-source-link/install.sh}
admin_key=${LITELLM_PROXY_ADMIN_KEY:-}
if [[ -z "$admin_key" ]]; then
    echo "set LITELLM_PROXY_ADMIN_KEY to a proxy-admin key for the $gateway gateway" >&2
    exit 2
fi

for path in "$binary" "$install_script"; do
    if [[ ! -f "$path" ]]; then
        echo "missing artifact: $path" >&2
        exit 2
    fi
done
if ! file "$binary" | grep -q 'statically linked'; then
    echo "refusing to publish $binary: it is not a statically linked binary" >&2
    exit 2
fi

sha=$(sha256sum "$binary" | cut -d' ' -f1)
size=$(stat -c %s "$binary")
release_json=$(mktemp)
payload=$(mktemp)
trap 'rm -f "$release_json" "$payload"' EXIT

printf '{"version":"%s","sha256":"%s","size":%s}' "$version" "$sha" "$size" >"$release_json"

# Build the request body with base64 of all three artifacts. Python is used only
# for base64 + JSON so a large binary never passes through a shell variable.
python_bin=${PYTHON:-$module/../.venv/bin/python}
if [[ ! -x "$python_bin" ]]; then
    python_bin=$(command -v python3 || true)
fi
if [[ -z "$python_bin" ]]; then
    echo "no python interpreter found; set PYTHON to one" >&2
    exit 2
fi
"$python_bin" - "$binary" "$release_json" "$install_script" >"$payload" <<'PY'
import base64, json, sys
binary, manifest, script = sys.argv[1:4]
def b64(path: str) -> str:
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("ascii")
with open(manifest) as handle:
    parsed = json.load(handle)
print(json.dumps({
    "version": parsed["version"],
    "sha256": parsed["sha256"],
    "binary_b64": b64(binary),
    "release_json_b64": b64(manifest),
    "install_script_b64": b64(script),
}))
PY

if [[ ${DRY_RUN:-0} == 1 ]]; then
    echo "dry run: would publish $version ($sha, $size bytes) to the $gateway gateway"
    echo "dry run: assets: source-link, release.json, install.sh; pointer moves last"
    exit 0
fi

response=$(curl -sS -w '\n%{http_code}' -X PUT \
    "http://127.0.0.1:${port}/install/admin/v1/source-agent/release" \
    -H "Authorization: Bearer ${admin_key}" \
    -H "Content-Type: application/json" \
    --data-binary "@${payload}")

status=${response##*$'\n'}
body=${response%$'\n'*}
if [[ "$status" != 200 ]]; then
    echo "publish failed with HTTP $status" >&2
    echo "$body" >&2
    exit 1
fi
echo "published $version to the $gateway gateway: $body"
