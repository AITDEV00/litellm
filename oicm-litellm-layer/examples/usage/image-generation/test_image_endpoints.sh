#!/usr/bin/env bash
# Exercise /v1/images/generations and /v1/images/edits through the gateway.
#
# Every case is saved under OUT_DIR/<endpoint>/<case>/, where <endpoint> is
# "generations" or "edits":
#   request.sh     the exact curl sent, with the bearer token redacted
#   response.json  the raw response, with any b64 payload elided
#   result.txt     http code, timing, response metadata, and the parameter note
#   output-N.png   each returned image, decoded
#
# Usage:
#   export PROXY_BASE_URL="https://litellm.ecouncil.ae"
#   export LITELLM_API_KEY="sk-..."
#   ./test_image_endpoints.sh                 # all cases
#   ./test_image_endpoints.sh gen-size-512    # one case
#   ./test_image_endpoints.sh gen-size-512 edit-mask
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
MASK="$INPUTS/mask-1024.png"
PORTRAIT="$INPUTS/portrait-452x678.png"

for f in "$P1" "$P2" "$P2_SMALL" "$MASK" "$PORTRAIT"; do
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
# run_case <name> <note> <curl args...>. The name is prefixed gen- or edit-, which
# selects the output subdirectory. The note names the parameter under test and
# lands in result.txt.
run_case() {
  local name="$1" note="$2"; shift 2
  local group=generations
  [[ "$name" == edit-* ]] && group=edits
  local case_dir="$OUT_DIR/$group/$name"
  mkdir -p "$case_dir"
  printf '%s\n' "$note" >"$case_dir/note.txt"
  LAST_REQUEST=("$@")
  echo "== $group/$name  [$note]"
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
# Each case exercises one request parameter so its effect is isolated.
declare -a ORDER=(
  # generations, JSON body
  gen-baseline
  gen-n-2
  gen-size-512
  gen-size-768
  gen-size-1024x768
  gen-steps-20
  gen-guidance-7-negative-prompt
  gen-seed-fixed
  gen-seed-list
  gen-output-format-webp
  gen-background-transparent
  gen-extra-body-steps
  gen-enhance-prompt
  gen-teacache
  gen-max-seq-len-256
  gen-flow-shift-3
  gen-generator-device-cpu
  gen-task-type-ti2i
  # edits, multipart form
  edit-baseline
  edit-two-images
  edit-mask
  edit-size-512
  edit-size-768
  edit-n-2
  edit-seed
  edit-steps-20
  edit-guidance-7-negative-prompt
  edit-background-transparent
  edit-output-format-webp
  edit-enhance-prompt
  edit-teacache
  edit-url
  edit-mixed-resolutions
  edit-portrait-grey-bg-black-suit
  # batching
  gen-concurrent-4
  gen-concurrent-mixed-size
)

# Every function below is named case_<case-name> so the dispatcher can call it.
# ------- generations -------
case_gen-baseline() {
  run_case gen-baseline "prompt only; defaults: 1024x1024, 40 steps, png" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"response_format\":\"b64_json\"}"
}

case_gen-n-2() {
  run_case gen-n-2 "n=2 -> two independent outputs" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"n\":2,\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-size-512() {
  run_case gen-size-512 "size=512x512" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-size-768() {
  run_case gen-size-768 "size=768x768 (verified working, above the picker's 512/1024 options)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"size\":\"768x768\",\"response_format\":\"b64_json\"}"
}

case_gen-size-1024x768() {
  run_case gen-size-1024x768 "size=1024x768 non-square aspect ratio" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"size\":\"1024x768\",\"response_format\":\"b64_json\"}"
}

case_gen-steps-20() {
  run_case gen-steps-20 "num_inference_steps=20 (default 40)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"num_inference_steps\":20,\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-guidance-7-negative-prompt() {
  run_case gen-guidance-7-negative-prompt "guidance_scale=7 + negative_prompt (CFG needs both)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"guidance_scale\":7,\"negative_prompt\":\"blurry, low quality, watermark\",\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-seed-fixed() {
  run_case gen-seed-fixed "seed=1234 fixed" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"seed\":1234,\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-seed-list() {
  run_case gen-seed-list "seed=[1,2] with n=2 (list length must equal n)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"n\":2,\"seed\":[1,2],\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-output-format-webp() {
  run_case gen-output-format-webp "output_format=webp (jpeg 500s: RGBA cannot encode as JPEG)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"output_format\":\"webp\",\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-background-transparent() {
  run_case gen-background-transparent "background=transparent is a NO-OP on generations (no alpha produced; use edits)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"background\":\"transparent\",\"output_format\":\"png\",\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-extra-body-steps() {
  run_case gen-extra-body-steps "extra_body passthrough for params LiteLLM filters (here num_inference_steps)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"size\":\"512x512\",\"seed\":42,\"response_format\":\"b64_json\",\"extra_body\":{\"num_inference_steps\":20,\"flow_shift\":3}}"
}

case_gen-enhance-prompt() {
  run_case gen-enhance-prompt "enhance_prompt=true" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"a capybara\",\"enhance_prompt\":true,\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-teacache() {
  run_case gen-teacache "enable_teacache=true -> accepted but output is byte-identical (no effect here)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"enable_teacache\":true,\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-max-seq-len-256() {
  run_case gen-max-seq-len-256 "max_sequence_length=256 -> accepted but output is byte-identical (no effect here)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"max_sequence_length\":256,\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-flow-shift-3() {
  run_case gen-flow-shift-3 "flow_shift=3 -> accepted but output is byte-identical (no effect here)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"flow_shift\":3,\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-generator-device-cpu() {
  run_case gen-generator-device-cpu "generator_device=cpu (backend-relative, not LiteLLM's own)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"generator_device\":\"cpu\",\"output_format\":\"png\",\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

case_gen-task-type-ti2i() {
  run_case gen-task-type-ti2i "task_type=TI2I (the pipeline's only supported type; T2I and text_to_image are rejected)" \
    -X POST "$PROXY_BASE_URL/v1/images/generations" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$MODEL\",\"prompt\":\"A capybara reading a book by candlelight\",\"task_type\":\"TI2I\",\"size\":\"512x512\",\"response_format\":\"b64_json\"}"
}

# ------- edits -------
case_edit-baseline() {
  run_case edit-baseline "single image + prompt" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue, keeping everything else unchanged.' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edit-two-images() {
  run_case edit-two-images "two images in one request" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Combine the subjects from Picture 1 and Picture 2 into one coherent scene.' \
    --form-string 'size=512x512' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png" -F "image[]=@$P2;type=image/png"
}

case_edit-mask() {
  run_case edit-mask "mask= is STRIPPED by LiteLLM for hosted_vllm (see PARAMS_VLLM_OMNI_DOES_NOT_ACCEPT)" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Replace the masked region with a blooming flower.' \
    --form-string 'size=512x512' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png" -F "mask=@$MASK;type=image/png"
}

case_edit-size-512() {
  run_case edit-size-512 "size=512x512" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue.' \
    --form-string 'size=512x512' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edit-size-768() {
  run_case edit-size-768 "size=768x768" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue.' \
    --form-string 'size=768x768' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edit-n-2() {
  run_case edit-n-2 "n=2 -> two outputs" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue.' \
    --form-string 'size=512x512' --form-string 'n=2' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edit-seed() {
  run_case edit-seed "seed=1234" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue.' \
    --form-string 'size=512x512' --form-string 'seed=1234' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edit-steps-20() {
  run_case edit-steps-20 "num_inference_steps=20" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue.' \
    --form-string 'size=512x512' --form-string 'num_inference_steps=20' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edit-guidance-7-negative-prompt() {
  run_case edit-guidance-7-negative-prompt "guidance_scale=7 + negative_prompt" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue.' \
    --form-string 'size=512x512' --form-string 'guidance_scale=7' \
    --form-string 'negative_prompt=blurry, low quality' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edit-background-transparent() {
  run_case edit-background-transparent "background=transparent -> real alpha" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue on a transparent background.' \
    --form-string 'size=512x512' --form-string 'background=transparent' \
    --form-string 'output_format=png' --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edit-output-format-webp() {
  run_case edit-output-format-webp "output_format=webp (jpeg 500s on RGBA output)" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue.' \
    --form-string 'size=512x512' --form-string 'output_format=webp' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edit-enhance-prompt() {
  run_case edit-enhance-prompt "enhance_prompt=true" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=teapot blue' \
    --form-string 'size=512x512' --form-string 'enhance_prompt=true' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edit-teacache() {
  run_case edit-teacache "enable_teacache=true -> accepted but output is byte-identical (no effect here)" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue.' \
    --form-string 'size=512x512' --form-string 'enable_teacache=true' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png"
}

case_edit-url() {
  run_case edit-url "url= instead of image[] -> HTTP 500, LiteLLM edits endpoint requires the image File param" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change the red teapot to blue.' \
    --form-string 'size=512x512' \
    --form-string "url=file://$P1" \
    --form-string 'response_format=b64_json'
}

case_edit-mixed-resolutions() {
  run_case edit-mixed-resolutions "inputs at 1024x1024 and 640x480" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Combine the subjects into one coherent scene.' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$P1;type=image/png" -F "image[]=@$P2_SMALL;type=image/png"
}

case_edit-portrait-grey-bg-black-suit() {
  run_case edit-portrait-grey-bg-black-suit \
    "portrait: background to #565656 grey and suit to fully black, subject otherwise unchanged" \
    -X POST "$PROXY_BASE_URL/v1/images/edits" \
    -H "Authorization: Bearer $LITELLM_API_KEY" -F "model=$MODEL" \
    --form-string 'prompt=Change only the background to a flat, uniform professional grey. Use exactly hex color #565656 (RGB 86, 86, 86) for the entire background. Make his suit fully black. Keep the subject unchanged otherwise, including his face and pose.' \
    --form-string 'output_format=png' \
    --form-string 'response_format=b64_json' \
    -F "image[]=@$PORTRAIT;type=image/png"
}

# ------- batching -------
# The scheduler merges only requests that share a batch signature, which
# includes every SamplingParams field (size, steps, guidance, seed, ...) except
# num_outputs_per_prompt. Edits can never merge: any request with image_path set
# is rejected with "image_conditioning".
run_concurrent_batch() {
  local name="$1" note="$2" sizes="$3"
  local case_dir="$OUT_DIR/generations/$name"
  mkdir -p "$case_dir"
  printf '%s\n' "$note" >"$case_dir/note.txt"
  echo "== generations/$name  [$note]"
  PROXY_BASE_URL="$PROXY_BASE_URL" LITELLM_API_KEY="$LITELLM_API_KEY" MODEL="$MODEL" \
    CASE_DIR="$case_dir" SIZES="$sizes" python3 - <<'PY'
import base64, concurrent.futures as cf, json, os, ssl, time, urllib.request

base, key, model = os.environ["PROXY_BASE_URL"], os.environ["LITELLM_API_KEY"], os.environ["MODEL"]
case_dir = os.environ["CASE_DIR"]
sizes = os.environ["SIZES"].split(",")
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE


def gen(i):
    size = sizes[i - 1]
    body = json.dumps({
        "model": model,
        "prompt": f"A photo of the number {i} painted on a wall",
        "size": size,
        "num_inference_steps": 40,
        "seed": 42,
        "response_format": "b64_json",
    }).encode()
    req = urllib.request.Request(
        f"{base}/v1/images/generations", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=600, context=ctx) as r:
            payload = json.loads(r.read())
        data = payload.get("data") or []
        img = data[0].get("b64_json") if data else None
        if img:
            open(os.path.join(case_dir, f"output-{i}.png"), "wb").write(base64.b64decode(img))
        return i, time.monotonic() - t0, bool(img), size, None
    except Exception as exc:
        return i, time.monotonic() - t0, False, size, str(exc)[:200]


t0 = time.monotonic()
with cf.ThreadPoolExecutor(max_workers=len(sizes)) as ex:
    results = list(ex.map(gen, range(1, len(sizes) + 1)))
wall = time.monotonic() - t0

lines = [f"wall_s={wall:.1f}", f"serial_estimate_s={sum(r[1] for r in results):.1f}",
         f"sizes={','.join(sizes)}"]
for i, el, ok, size, err in results:
    lines.append(f"req{i} size={size}: {el:.1f}s ok={ok}" + (f" err={err}" if err else ""))
open(os.path.join(case_dir, "result.txt"), "w").write("\n".join(lines) + "\n")

open(os.path.join(case_dir, "request.sh"), "w").write(
    "curl -sS --fail-with-body \\\n"
    "  -X POST $PROXY_BASE_URL/v1/images/generations \\\n"
    "  -H 'Authorization: Bearer <REDACTED>' \\\n"
    "  -H 'Content-Type: application/json' \\\n"
    "  -d '{\"model\":\"<MODEL>\",\"prompt\":\"<per-request>\",\"size\":\"<see sizes>\","
    "\"num_inference_steps\":40,\"seed\":42,\"response_format\":\"b64_json\"}'\n"
    f"# fired {len(sizes)}x concurrently, sizes={','.join(sizes)}\n"
)
print(f"    wall={wall:.1f}s  serial_est={sum(r[1] for r in results):.1f}s  "
      f"ok={sum(1 for r in results if r[2])}/{len(sizes)}")
PY
}

case_gen-concurrent-4() {
  run_concurrent_batch gen-concurrent-4 \
    "4 concurrent generations, same params and same 1024x1024 size; expect a 4/4 merge" \
    "1024x1024,1024x1024,1024x1024,1024x1024"
}

case_gen-concurrent-mixed-size() {
  run_concurrent_batch gen-concurrent-mixed-size \
    "4 concurrent generations with differing sizes; batch signature mismatch, expect no merge" \
    "512x512,768x768,1024x1024,512x512"
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
echo "done. inspect $OUT_DIR/{generations,edits}/<case>/{request.sh,result.txt,output-*.png}"
