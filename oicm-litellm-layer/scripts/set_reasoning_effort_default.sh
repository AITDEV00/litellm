#!/usr/bin/env bash
# Set the operator-level default reasoning_effort on a DB-stored deployment.
#
# Writes litellm_params.reasoning_effort, which the Router merges into every
# request that does not carry its own effort. This is the only field that
# actually defaults reasoning effort; model_info.default_reasoning_effort is
# advisory metadata read by the OpenAI GPT-5 param guards and never injects.
#
# The value is per-model: a tier another model accepts can 400 here. The proxy
# advertises the accepted set in model_info.reasoning_effort_levels, which this
# script prints alongside the current value so you can pick a valid one.
#
# DRY RUN by default. Pass --apply to write. The write is always sent rather
# than skipped when a read already shows the requested value, because
# /model/info is served from a cache that lags writes by a few seconds and can
# report a stale value on the replica that answers. Re-sending an identical
# value is harmless; skipping on a stale read silently leaves the old value.
#
# Usage:
#   export PROXY_BASE_URL="https://litellm.ecouncil.ae"
#   export LITELLM_API_KEY="sk-..."
#   ./set_reasoning_effort_default.sh deepseek-ai/DeepSeek-V4.1-Flash xhigh
#   ./set_reasoning_effort_default.sh ffc021e6-832e-4e83-b9a9-40cb20107a92 xhigh --apply
#
# Positional:
#   1  model_name or model_info.id of the deployment
#   2  reasoning effort tier to store (none|minimal|low|medium|high|xhigh|max)
#
# Requires: curl, jq (>=1.6)

set -euo pipefail

APPLY=0
EFFORT_TIERS="none minimal low medium high xhigh max"

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    -h|--help) usage 0 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
if (( ${#POSITIONAL[@]} != 2 )); then
  echo "expected exactly 2 positional args: <model-name-or-id> <effort>" >&2
  usage 1
fi
TARGET="${POSITIONAL[0]}"
EFFORT="${POSITIONAL[1]}"

: "${PROXY_BASE_URL:?set PROXY_BASE_URL}"
: "${LITELLM_API_KEY:?set LITELLM_API_KEY}"

command -v curl >/dev/null && command -v jq >/dev/null || { echo "curl and jq are required" >&2; exit 1; }

case " $EFFORT_TIERS " in
  *" $EFFORT "*) ;;
  *) echo "unknown effort tier: '$EFFORT' (expected one of: $EFFORT_TIERS)" >&2; exit 1 ;;
esac

CURL=(curl -sk -H "Authorization: Bearer $LITELLM_API_KEY" -H "Content-Type: application/json")

http() {
  local method="$1" path="$2" body="${3:-}"
  local out code
  if [[ -n "$body" ]]; then
    out=$("${CURL[@]}" -X "$method" "$PROXY_BASE_URL$path" -d "$body" -w $'\n%{http_code}')
  else
    out=$("${CURL[@]}" -X "$method" "$PROXY_BASE_URL$path" -w $'\n%{http_code}')
  fi
  code="${out##*$'\n'}"
  out="${out%$'\n'*}"
  if [[ "$code" != 2* ]]; then
    echo "ERROR: $method $path -> HTTP $code: ${out:0:300}" >&2
    return 1
  fi
  printf '%s' "$out"
}

# ---------------------------------------------------------------------------
# Resolve the deployment. An exact id wins; otherwise match model_name and
# refuse to guess when several deployments share it.
# ---------------------------------------------------------------------------
MODELS=$(http GET /model/info)

MATCH=$(jq -c --arg t "$TARGET" '
  [ .data[]
    | select(.model_info.id == $t or .model_name == $t)
    | { id: .model_info.id,
        name: .model_name,
        current: (.litellm_params.reasoning_effort // null),
        levels: (.model_info.reasoning_effort_levels // []) } ]' <<<"$MODELS")

MATCH_COUNT=$(jq 'length' <<<"$MATCH")
if [[ "$MATCH_COUNT" == 0 ]]; then
  echo "ERROR: no deployment matches '$TARGET' by model_name or model_info.id" >&2
  exit 1
fi
if [[ "$MATCH_COUNT" != 1 ]]; then
  echo "ERROR: '$TARGET' is ambiguous, $MATCH_COUNT deployments match. Pass an id:" >&2
  jq -r '.[] | "  \(.id)  \(.name)"' <<<"$MATCH" >&2
  exit 1
fi

MODEL_ID=$(jq -r '.[0].id' <<<"$MATCH")
MODEL_NAME=$(jq -r '.[0].name' <<<"$MATCH")
CURRENT=$(jq -r '.[0].current' <<<"$MATCH")
LEVELS=$(jq -r '.[0].levels | if length > 0 then join(", ") else "(not advertised)" end' <<<"$MATCH")

echo "=== set reasoning_effort default ==="
echo "proxy:       $PROXY_BASE_URL"
echo "deployment:  $MODEL_NAME"
echo "model id:    $MODEL_ID"
echo "current:     ${CURRENT}"
echo "advertised:  $LEVELS"
echo "requested:   $EFFORT"
echo "mode:        $([[ $APPLY == 1 ]] && echo APPLY || echo DRY-RUN)"
echo

# /model/info answers from a per-replica cache that lags writes by a few
# seconds, so one read can disagree with the stored row. Sample a few and treat
# agreement as the signal.
read_effort() {
  http GET /model/info \
    | jq -r --arg id "$MODEL_ID" '.data[] | select(.model_info.id == $id) | (.litellm_params.reasoning_effort // "<unset>")'
}

sample_effort() {
  local n="${1:-3}" i
  for (( i = 0; i < n; i++ )); do
    read_effort
    sleep 1
  done
}

OBSERVED=$(sample_effort 3 | sort -u)
if [[ $(wc -l <<<"$OBSERVED" | tr -d ' ') -gt 1 ]]; then
  echo "note: reads disagree ($(tr '\n' ' ' <<<"$OBSERVED"| sed 's/ $//')), the read cache is mid-lag"
  echo
fi

if [[ $APPLY != 1 ]]; then
  echo "dry-run only. Re-run with --apply to store reasoning_effort='$EFFORT'."
  exit 0
fi

# ---------------------------------------------------------------------------
# Apply. A minimal body is deliberate: PATCH merges over the stored row, so
# sending only this key leaves model, api_base, pricing, and every other param
# untouched. It is also what keeps the value reconcile-safe, since the OICM
# controller patches the same way and never sends reasoning_effort.
# ---------------------------------------------------------------------------
BODY=$(jq -n --arg e "$EFFORT" '{litellm_params: {reasoning_effort: $e}}')
http PATCH "/model/$MODEL_ID/update" "$BODY" >/dev/null
echo "patched $MODEL_ID -> reasoning_effort='$EFFORT'"

# ---------------------------------------------------------------------------
# Verify. Require two consecutive reads to agree, so a lone stale hit on a
# lagging replica cannot pass the check.
# ---------------------------------------------------------------------------
echo
echo "=== verification ==="
prev=""
attempt=1
while (( attempt <= 15 )); do
  observed=$(read_effort)
  if [[ "$observed" == "$EFFORT" && "$prev" == "$EFFORT" ]]; then
    echo "RESULT: verified reasoning_effort='$EFFORT' on $MODEL_ID (after ${attempt} read(s))."
    exit 0
  fi
  prev="$observed"
  sleep 1
  attempt=$(( attempt + 1 ))
done

echo "RESULT: write did not take. expected '$EFFORT', read '$observed' after 15 reads." >&2
exit 1
