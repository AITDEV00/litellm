#!/usr/bin/env bash
# Generate an image through the gateway and save it as a PNG.
#
# Usage:
#   export PROXY_BASE_URL="https://litellm.ecouncil.ae"
#   export LITELLM_API_KEY="sk-..."
#   ./gen_image.sh "A capybara reading a book by candlelight"
#   ./gen_image.sh "A red teapot on a wooden table" /tmp/teapot.png
#   MODEL=Qwen/Qwen-Image-2.1 OUT_DIR=/tmp ./gen_image.sh "a prompt"
#
# Positional:
#   1  prompt (required)
#   2  output png path (optional; default OUT_DIR/<slug>-<timestamp>.png)
#
# Env: MODEL (default Qwen/Qwen-Image-2.1), OUT_DIR (default ./out/generations, gitignored)
#
# Requires: curl, python3

set -euo pipefail

PROMPT="${1:?usage: gen_image.sh \"<prompt>\" [output.png]}"
OUT="${2:-}"

: "${PROXY_BASE_URL:?set PROXY_BASE_URL}"
: "${LITELLM_API_KEY:?set LITELLM_API_KEY}"

MODEL="${MODEL:-Qwen/Qwen-Image-2.1}"
OUT_DIR="${OUT_DIR:-$(cd "$(dirname "$0")" && pwd)/out/generations}"
mkdir -p "$OUT_DIR"

if [[ -z "$OUT" ]]; then
  slug=$(printf '%s' "$PROMPT" | tr -cs '[:alnum:]' '-' | cut -c1-40 | sed 's/^-//; s/-$//')
  OUT="$OUT_DIR/${slug:-image}-$(date +%Y%m%d-%H%M%S).png"
fi

BODY=$(python3 -c 'import json,sys; print(json.dumps({"model":sys.argv[1],"prompt":sys.argv[2],"response_format":"b64_json"}))' "$MODEL" "$PROMPT")

echo "model:  $MODEL"
echo "prompt: $PROMPT"

curl -sS --fail-with-body -X POST "$PROXY_BASE_URL/v1/images/generations" \
  -H "Authorization: Bearer $LITELLM_API_KEY" \
  -H 'Content-Type: application/json' \
  -d "$BODY" \
  -o "$OUT.json" \
  -w 'http:   %{http_code}  (%{time_total}s)\n'

python3 - "$OUT.json" "$OUT" <<'PY'
import base64, json, sys

raw, out = sys.argv[1], sys.argv[2]
try:
    payload = json.load(open(raw))
except Exception as exc:
    raise SystemExit(f"response was not JSON ({exc}): {open(raw, 'rb').read()[:300]!r}")

if isinstance(payload.get("error"), (dict, str)):
    raise SystemExit(f"gateway error: {json.dumps(payload['error'])[:500]}")

data = payload.get("data") or []
if not data:
    raise SystemExit(f"no image in response: {json.dumps(payload)[:500]}")
b64 = data[0].get("b64_json")
if not b64:
    raise SystemExit(f"no b64_json in response: {json.dumps(payload)[:500]}")

with open(out, "wb") as fh:
    fh.write(base64.b64decode(b64))
print(f"saved:  {out}  ({len(b64)} b64 chars)")
PY

rm -f "$OUT.json"
