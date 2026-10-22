#!/usr/bin/env bash
# Exercises every model in docs/usage-guides through the gateway using a
# virtual key, reproducing the exact request shapes those guides document.
# Master keys bypass allowed_routes, so a master key cannot prove a customer
# key works; this script exists to be run with a real key_type=llm_api key.
set -uo pipefail

BASE="${BASE:-https://litellm.ecouncil.ae}"
KEY="${KEY:?set KEY to a virtual gateway key}"
AUDIO="${AUDIO:-/home/jyao/ADEO/litellm/tests/proxy_unit_tests/gettysburg.wav}"
SHORT_AUDIO="${SHORT_AUDIO:-$AUDIO}"
OUT="${OUT:-/tmp/usage-guide-verify}"
mkdir -p "$OUT"

pass=0
fail=0
declare -a failed=()

check() {
  local name="$1" expect="$2" got="$3" detail="${4:-}"
  if [[ "$got" == "$expect" ]]; then
    printf '  PASS  %-42s %s\n' "$name" "$got"
    pass=$((pass + 1))
  else
    printf '  FAIL  %-42s got=%s want=%s %s\n' "$name" "$got" "$expect" "$detail"
    fail=$((fail + 1))
    failed+=("$name")
  fi
}

post_json() {
  local path="$1" body="$2" out="$3"
  curl -s -o "$out" -w '%{http_code}' -X POST "$BASE$path" \
    -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -d "$body"
}

post_form() {
  local path="$1" out="$2"
  shift 2
  curl -s -o "$out" -w '%{http_code}' -X POST "$BASE$path" \
    -H "Authorization: Bearer $KEY" "$@"
}

echo "== GATEWAY_GUIDE.md =="

check "GET /v1/models" 200 "$(curl -s -o "$OUT/models.json" -w '%{http_code}' "$BASE/v1/models" -H "Authorization: Bearer $KEY")"
python3 - "$OUT/models.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    ids = sorted(m.get("id", "") for m in d.get("data", []))
    print(f"  models visible to this key: {len(ids)}")
except Exception as e:
    print(f"  could not parse model list: {e}")
PY

for m in "Qwen/Qwen3-Next-80B-A3B-Instruct" "deepseek-ai/DeepSeek-V4.1-Flash" \
         "Qwen/Qwen3.5-122B-A10B-GPTQ-Int4" "Qwen3.6-35B-A3B-FP8" \
         "Qwen/Qwen3.8-Flash-Next-FP8" "deepseek-ai/DeepSeek-V4-Flash-0731"; do
  body=$(printf '{"model":"%s","messages":[{"role":"user","content":"Say hello in one word."}],"max_tokens":64}' "$m")
  check "chat $m" 200 "$(post_json /v1/chat/completions "$body" "$OUT/chat.json")" "$(head -c 120 "$OUT/chat.json")"
done

for m in "Qwen/Qwen3-Embedding-4B" "Qwen/Qwen3-Embedding-0.6B"; do
  body=$(printf '{"model":"%s","input":"Hello world"}' "$m")
  check "embeddings $m" 200 "$(post_json /v1/embeddings "$body" "$OUT/emb.json")" "$(head -c 120 "$OUT/emb.json")"
done

check "embeddings batch" 200 "$(post_json /v1/embeddings '{"model":"Qwen/Qwen3-Embedding-0.6B","input":["a","b","c"]}' "$OUT/emb_batch.json")"

echo
echo "== HAMSA-TTS-NEW-V1-GUIDE.md =="
check "tts hamsa-tts-new (Zeina)" 200 "$(post_json /v1/audio/speech '{"model":"hamsa-tts-new","input":"Hello world, this is a text-to-speech test.","voice":"Zeina","response_format":"wav"}' "$OUT/hamsa_tts.wav")" "$(head -c 120 "$OUT/hamsa_tts.wav")"
check "tts hamsa-tts-new (Amir)" 200 "$(post_json /v1/audio/speech '{"model":"hamsa-tts-new","input":"Hello world.","voice":"Amir","response_format":"wav"}' "$OUT/hamsa_tts_amir.wav")"
check "tts hamsa-tts-new (adeo_mariam)" 200 "$(post_json /v1/audio/speech '{"model":"hamsa-tts-new","input":"Hello world.","voice":"adeo_mariam","response_format":"wav"}' "$OUT/hamsa_tts_adeo.wav")"

echo
echo "== INCEPTION_TTS_STT_GUIDE.md =="
check "tts inception-tts" 200 "$(post_json /v1/audio/speech '{"model":"inception-tts","input":"Hello world, this is a text-to-speech test.","voice":"alloy","response_format":"mp3"}' "$OUT/inception_tts.mp3")" "$(head -c 80 "$OUT/inception_tts.mp3")"
check "stt inception-stt" 200 "$(post_form /v1/audio/transcriptions "$OUT/inception_stt.json" -F "model=inception-stt" -F "file=@$AUDIO" -F "language=en")" "$(head -c 160 "$OUT/inception_stt.json")"

echo
echo "== HAMSA_STT_GUIDE.md =="
check "stt hamsa-stt" 200 "$(post_form /v1/audio/transcriptions "$OUT/hamsa_stt.json" -F "model=hamsa-stt" -F "file=@$AUDIO" -F "language=auto")" "$(head -c 160 "$OUT/hamsa_stt.json")"

echo
echo "== OMNIVOICE_TTS_GUIDE.md =="
check "tts omnivoice" 200 "$(post_json /v1/audio/speech '{"model":"omnivoice","input":"Hello world, this is a text-to-speech test.","voice":"alloy","response_format":"mp3"}' "$OUT/omnivoice.mp3")" "$(head -c 80 "$OUT/omnivoice.mp3")"
check "tts omnivoice defaults" 200 "$(post_json /v1/audio/speech '{"model":"omnivoice","input":"Hello world."}' "$OUT/omnivoice_default.mp3")"
check "tts omnivoice custom params" 200 "$(post_json /v1/audio/speech '{"model":"omnivoice","input":"Hello world with custom params.","voice":"alloy","response_format":"wav","speed":1.0,"num_step":32,"guidance_scale":3.0,"denoise":true}' "$OUT/omnivoice_custom.wav")"
check "clone omnivoice" 200 "$(post_form /v1/audio/speech/clone "$OUT/omnivoice_clone.wav" -F "model=omnivoice" -F "text=Hello from a cloned voice." -F "ref_audio=@$AUDIO" -F "ref_text=Four score and seven years ago." -F "response_format=wav")" "$(head -c 160 "$OUT/omnivoice_clone.wav")"

echo
echo "== PYANNOTE_DIARIZATION_GUIDE.md =="
check "diarize pyannote/community-1" 200 "$(post_form /v1/diarize "$OUT/diarize.json" -F "model=pyannote/community-1" -F "exclusive=true" -F "file=@$AUDIO")" "$(head -c 160 "$OUT/diarize.json")"
check "diarize model omitted (default)" 200 "$(post_form /v1/diarize "$OUT/diarize_default.json" -F "file=@$AUDIO")"
check "diarize per-field 400 passthrough" 400 "$(post_form /v1/diarize "$OUT/diarize_400.json" -F "model=pyannote/community-1" -F "file=@$AUDIO" -F "numSpeakers=2")" "$(head -c 160 "$OUT/diarize_400.json")"

echo
echo "== VIBEVOICE_ASR_GUIDE.md =="
python3 - "$AUDIO" "$OUT/vibevoice_body.json" <<'PY'
import base64, json, sys
audio_path, out_path = sys.argv[1], sys.argv[2]
b64 = base64.b64encode(open(audio_path, "rb").read()).decode()
body = {
    "model": "microsoft/VibeVoice-ASR",
    "messages": [
        {"role": "system", "content": "You are a helpful assistant that transcribes audio input into text output in JSON format."},
        {"role": "user", "content": [
            {"type": "text", "text": "Transcribe this audio."},
            {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}},
        ]},
    ],
    "max_tokens": 512,
}
open(out_path, "w").write(json.dumps(body))
PY
check "asr microsoft/VibeVoice-ASR" 200 "$(curl -s -o "$OUT/vibevoice.json" -w '%{http_code}' -X POST "$BASE/v1/chat/completions" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' --data-binary "@$OUT/vibevoice_body.json")" "$(head -c 160 "$OUT/vibevoice.json")"

echo
echo "== REASONING_CONTROL_GUIDE.md =="
for m in "deepseek-ai/DeepSeek-V4.1-Flash" "moonshotai/Kimi-K3" "zai-org/GLM-5.3-Flash" "Qwen/Qwen3.8-Flash-Next-FP8"; do
  body=$(printf '{"model":"%s","messages":[{"role":"user","content":"What is 2+2?"}],"reasoning_effort":"none","max_tokens":128}' "$m")
  slug=$(printf '%s' "$m" | tr '/' '_')
  check "reasoning_effort=none $m" 200 "$(post_json /v1/chat/completions "$body" "$OUT/reason_$slug.json")" "$(head -c 100 "$OUT/reason_$slug.json")"
done

echo
echo "== auth negatives =="
check "no auth -> 401" 401 "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/diarize" -F "model=pyannote/community-1" -F "file=@$AUDIO")"
check "bad key -> 401" 401 "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/v1/diarize" -H "Authorization: Bearer sk-not-a-key" -F "model=pyannote/community-1" -F "file=@$AUDIO")"

echo
echo "================ SUMMARY ================"
printf 'pass=%d fail=%d\n' "$pass" "$fail"
if ((${#failed[@]})); then
  printf 'failed:\n'
  printf '  - %s\n' "${failed[@]}"
  exit 1
fi
echo "all checks passed"
