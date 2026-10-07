#!/usr/bin/env bash
# Exercise /v1/images/generations and /v1/images/edits through the gateway.
#
# For every case it saves, under OUT_DIR/<case>/:
#   request.sh     the exact curl sent, with the bearer token redacted
#   response.json  the raw response, with any b64 payload elided
#   result.txt     http code, timing, and decoded image dimensions
#   output-N.png   each returned image, decoded
#
# Usage:
#   export PROXY_BASE_URL="https://litellm.ecouncil.ae"
#   export LITELLM_API_KEY="sk-..."
#   ./test_image_endpoints.sh                 # all cases
#   ./test_image_endpoints.sh edits-two-images-transparent   # one case
#
# Env: MODEL (default Qwen/Qwen-Image-2.1), OUT_DIR (default ./out)
#
# Requires: curl, python3

set -uo pipefail

: "${PROXY_BASE_URL:?set PROXY_BASE_URL}"
: "${LITELLM_API_KEY:?set LITELLM_API_KEY}"

MODEL="${MODEL:-Qwen/Qwen-Image-2.1}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${OUT_DIR:-$HERE/out}"
INPUTS="$HERE/inputs"

P1="$INPUTS/picture-1-1024.png"
P2="$INPUTS/picture-2-1024.png"
P2_SMALL="$INPUTS/picture-2-640x480.png"

for f in "$P1" "$P2" "$P2_SMALL"; do
  [[ -f "$f" ]] || { echo "missing input image: $f" >&2; exit 1; }
done

# --- request recording ------------------------------------------------------
# LAST_REQUEST holds the argv of the curl call; redact_auth rewrites it for
# saving. The token never appears in anything written to disk.
LAST_REQUEST=()

redact_auth() {
  python3 - "$@" <<'PY'
import sys

args = sys.argv[1:]
out, skip_next = [], False
for i, a in enumerate(args):
    if skip_next:
        skip_next = False
        continue
    if a == "-H" and i + 1 < len(args) and args[i + 1].lower().startswith("authorization:"):
        out.append(a)
        out.append("Authorization: Bearer <REDACTED>")
        skip_next = True
        continue
    out.append(a)

print("curl -sS --fail-with-body \\")
for a in out:
    print(f"  {a!r} \\" if any(c in a for c in " '\"{}[]|") else f"  {a} \\")
print("  -o response.json")
PY
}

# --- decoding ---------------------------------------------------------------
# Writes output-N.png per image, a b64-elided response.json, and result.txt.
decode() {
  local case_dir="$1" http_code="$2" elapsed="$3"
  python3 - "$case_dir" "$http_code" "$elapsed" <<'PY'
import base64, json, os, sys


def main():
    case_dir, http_code, elapsed = sys.argv[1], sys.argv[2], sys.argv[3]
    raw = os.path.join(case_dir, "response.raw.json")
    try:
        payload = json.load(open(raw))
    except Exception as exc:
        open(os.path.join(case_dir, "result.txt"), "w").write(
            f"http={http_code} elapsed={elapsed}\nnot JSON: {exc}\n"
            f"body[:500]={open(raw, 'rb').read()[:500]!r}\n"
        )
        print(f"    http={http_code}  NOT JSON ({exc})")
        return

    lines = [f"http={http_code}", f"elapsed_s={elapsed}"]

    err = payload.get("error")
    if err is not None:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        lines.append(f"error={msg}")
        print(f"    http={http_code}  ERROR: {str(msg)[:160]}")
    elif isinstance(payload.get("data"), list):
        elided = dict(payload)
        dims = []
        for idx, item in enumerate(payload["data"], start=1):
            b64 = item.get("b64_json") if isinstance(item, dict) else None
            if b64:
                png = base64.b64decode(b64)
                path = os.path.join(case_dir, f"output-{idx}.png")
                with open(path, "wb") as fh:
                    fh.write(png)
                dims.append(f"{os.path.basename(path)}={len(png)}B")
                elided["data"][idx - 1] = {
                    k: ("<b64 elided, see output-%d.png>" % idx) if k == "b64_json" else v
                    for k, v in item.items()
                }
            else:
                dims.append(f"data[{idx}] has no b64_json")
        lines.append(f"images={len(payload['data'])}")
        lines.extend(dims)
        for k, v in payload.items():
            if k != "data":
                lines.append(f"{k}={json.dumps(v)}")
        print(f"    http={http_code}  images={len(payload['data'])}  {'  '.join(dims)}")
        json.dump(elided, open(os.path.join(case_dir, "response.json"), "w"), indent=2)
    else:
        lines.append("no data array and no error")
        print(f"    http={http_code}  unexpected shape: {json.dumps(payload)[:200]}")

    open(os.path.join(case_dir, "result.txt"), "w").write("\n".join(lines) + "\n")


main()
PY
  rm -f "$case_dir/response.raw.json"
}

# --- one case ---------------------------------------------------------------
run_case() {
  local name="$1"; shift
  local case_dir="$OUT_DIR/$name"
  mkdir -p "$case_dir"
  LAST_REQUEST=("$@")
  echo "== $name"
  local out
  out=$(curl -sS --fail-with-body "${LAST_REQUEST[@]}" \
        -o "$case_dir/response.raw.json" \
        -w '%{http_code} %{time_total}' 2>"$case_dir/curl.stderr") || true
  local http_code="${out%% *}" elapsed="${out##* }"
  [[ -s "$case_dir/curl.stderr" ]] && cat "$case_dir/curl.stderr" >>"$case_dir/result.txt"
  rm -f "$case_dir/curl.stderr"
  redact_auth "${LAST_REQUEST[@]}" >"$case_dir/request.sh"
  decode "$case_dir" "${http_code:-000}" "${elapsed:-?}"
}

# --- cases ------------------------------------------------------------------
# Cases are selected by name; with no args every case runs.
declare -a ORDER=(
  generations-basic
  edits-single-transparent
  edits-two-images-transparent
  edits-two-images-size-512
  edits-two-images-scene-512
  edits-two-images-n2
  edits-mixed-resolutions
)

case_generations-basic() {
  run_case generations-basic \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"generator_device\":\"cpu\",\"output_format\":\"png\",\"response_format\":\"b64_json\"}"
}

case_edits-single-transparent() {
  run_case edits-single-transparent \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" \
    -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue, keeping its shape, table, window, and lighting unchanged.' \
    --form-string 'generator_device=cpu' \
    --form-string 'output_format=png' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edits-two-images-transparent() {
  run_case edits-two-images-transparent \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" \
    -F "model=$MODEL" \
    --form-string 'prompt=Combine the subjects from Picture 1 and Picture 2 into one composition on a transparent background. Preserve an alpha channel outside the subjects.' \
    --form-string 'generator_device=cpu' \
    --form-string 'output_format=png' \
    --form-string 'response_format=b64_json' \
    --form-string 'background=transparent' \
    -F "image[]=@$P1;type=image/png" \
    -F "image[]=@$P2;type=image/png"
}

case_edits-two-images-size-512() {
  run_case edits-two-images-size-512 \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" \
    -F "model=$MODEL" \
    --form-string 'prompt=Combine the subjects from Picture 1 and Picture 2 into one composition on a transparent background. Preserve an alpha channel outside the subjects.' \
    --form-string 'generator_device=cpu' \
    --form-string 'output_format=png' \
    --form-string 'response_format=b64_json' \
    --form-string 'size=512x512' \
    --form-string 'background=transparent' \
    -F "image[]=@$P1;type=image/png" \
    -F "image[]=@$P2;type=image/png"
}

case_edits-two-images-scene-512() {
  run_case edits-two-images-scene-512 \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" \
    -F "model=$MODEL" \
    --form-string 'prompt=Combine the subjects from Picture 1 and Picture 2 into one coherent scene, preserving their appearance.' \
    --form-string 'generator_device=cpu' \
    --form-string 'output_format=png' \
    --form-string 'response_format=b64_json' \
    --form-string 'size=512x512' \
    -F "image[]=@$P1;type=image/png" \
    -F "image[]=@$P2;type=image/png"
}

case_edits-two-images-n2() {
  run_case edits-two-images-n2 \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" \
    -F "model=$MODEL" \
    --form-string 'prompt=Combine the subjects from Picture 1 and Picture 2 into one coherent scene, preserving their appearance.' \
    --form-string 'generator_device=cpu' \
    --form-string 'output_format=png' \
    --form-string 'response_format=b64_json' \
    --form-string 'size=512x512' \
    --form-string 'n=2' \
    -F "image[]=@$P1;type=image/png" \
    -F "image[]=@$P2;type=image/png"
}

case_edits-mixed-resolutions() {
  run_case edits-mixed-resolutions \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" \
    -F "model=$MODEL" \
    --form-string 'prompt=Combine the subjects from Picture 1 and Picture 2 into one coherent scene, preserving their appearance.' \
    --form-string 'generator_device=cpu' \
    --form-string 'output_format=png' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png" \
    -F "image[]=@$P2_SMALL;type=image/png"
}

SELECTED=("$@")
if (( ${#SELECTED[@]} == 0 )); then SELECTED=("${ORDER[@]}"); fi

echo "model:   $MODEL"
echo "out:     $OUT_DIR"
echo
for name in "${SELECTED[@]}"; do
  fn="case_$name"
  if ! declare -F "$fn" >/dev/null; then
    echo "unknown case: $name" >&2
    exit 1
  fi
  "$fn"
done
echo
echo "done. inspect $OUT_DIR/<case>/request.sh, result.txt, output-*.png"
