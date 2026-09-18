#!/usr/bin/env bash
# Add one model id to organization, team, and API-key model access lists that
# already contain any of the given trigger model ids. Exact matching on both
# trigger and target; wildcards are never expanded. A target is only appended
# when absent from the list; objects without any trigger model are untouched.
#
# DRY RUN by default. Pass --apply to write changes.
#
# Usage:
#   export PROXY_BASE_URL="https://litellm.ecouncil.ae"
#   export LITELLM_API_KEY="sk-..."
#   ./add_model_if_has_refs.sh "zai-org/GLM-5.3,zai-org/GLM-5.3-Flash" "zai-org/GLM-5.4"
#   ./add_model_if_has_refs.sh "zai-org/GLM-5.3" "zai-org/GLM-5.4" --apply
#   ./add_model_if_has_refs.sh "zai-org/GLM-5.3" "zai-org/GLM-5.4" --apply --only keys
#
# Positional:
#   1  comma-separated trigger model ids; any one present in an access list qualifies it
#   2  model id to add to every qualifying access list
#
# Requires: curl, jq (>=1.6)

set -euo pipefail

APPLY=0
ONLY="all"

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --only)  ONLY="${2:?scope required: orgs|teams|keys}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
if (( ${#POSITIONAL[@]} != 2 )); then
  echo "expected exactly 2 positional args: <trigger-models> <model-to-add>" >&2
  usage 1
fi
TRIGGERS="${POSITIONAL[0]}"
ADD_MODEL="${POSITIONAL[1]}"

: "${PROXY_BASE_URL:?set PROXY_BASE_URL}"
: "${LITELLM_API_KEY:?set LITELLM_API_KEY}"

command -v curl >/dev/null && command -v jq >/dev/null || { echo "curl and jq are required" >&2; exit 1; }

case "$ONLY" in all|orgs|teams|keys) ;; *) echo "invalid --only: $ONLY" >&2; exit 1 ;; esac

TRIG_JSON=$(jq -Rn --arg t "$TRIGGERS" '[$t | split(",")[] | gsub("^ +| +$";"")] | map(select(length > 0))')
if [[ $(jq 'length' <<<"$TRIG_JSON") -eq 0 ]]; then
  echo "no trigger models parsed from: '$TRIGGERS'" >&2
  exit 1
fi
if [[ -z "$ADD_MODEL" ]]; then
  echo "model-to-add must not be empty" >&2
  exit 1
fi
if [[ "$ADD_MODEL" == *"*"* ]]; then
  echo "wildcard targets are not supported: '$ADD_MODEL'" >&2
  exit 1
fi

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
# Fetch valid model names + all three levels
# ---------------------------------------------------------------------------
VALID=$(http GET /model/info | jq -c '[.data[].model_name] | unique')
if ! jq -e --arg a "$ADD_MODEL" '. | contains([$a])' <<<"$VALID" >/dev/null; then
  echo "ERROR: model to add is not registered on the proxy: '$ADD_MODEL'" >&2
  exit 1
fi

fetch_keys() {
  local page=1 total_pages=1 chunk
  local all="[]"
  while (( page <= total_pages )); do
    chunk=$(http GET "/key/list?page=$page&size=100&return_full_object=true")
    total_pages=$(jq '.total_pages // 1' <<<"$chunk")
    all=$(jq -c --argjson acc "$all" '$acc + .keys' <<<"$chunk")
    page=$(( page + 1 ))
  done
  printf '%s' "$all"
}

ORGS=$(http GET /organization/list)
TEAMS=$(http GET /team/list)
KEYS=$(fetch_keys)

# ---------------------------------------------------------------------------
# Qualification. An object qualifies when its access list contains at least one
# trigger id and does not already contain the target id.
# ---------------------------------------------------------------------------
JQ_FILTER='
  .[]
  | (.models // []) as $ms
  | select( any($ms[]; . as $m | $T | contains([$m])) and ($ms | contains([$A]) | not) )
  | { level: $level,
      id: .[$idfield],
      name: .[$namefield],
      models: $ms }
'

plan_for() {
  local level="$1" json="$2" idfield="$3" namefield="$4"
  jq -c --argjson T "$TRIG_JSON" --arg A "$ADD_MODEL" \
    --arg level "$level" --arg idfield "$idfield" --arg namefield "$namefield" \
    "$JQ_FILTER" <<<"$json" || { echo "ERROR: scan filter failed for $level" >&2; exit 1; }
}

collect_plans() {
  local plans=""
  if [[ "$ONLY" == all || "$ONLY" == orgs ]]; then
    plans+="$(plan_for orgs "$ORGS" organization_id organization_alias)"$'\n'
  fi
  if [[ "$ONLY" == all || "$ONLY" == teams ]]; then
    plans+="$(plan_for teams "$TEAMS" team_id team_alias)"$'\n'
  fi
  if [[ "$ONLY" == all || "$ONLY" == keys ]]; then
    plans+="$(plan_for keys "$KEYS" token key_alias)"$'\n'
  fi
  printf '%s' "$plans"
}

PLANS=$(collect_plans | grep . || true)
PLAN_COUNT=$(wc -l <<<"$PLANS" | tr -d ' ')
[[ -z "$PLANS" ]] && PLAN_COUNT=0

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
echo "=== add-model-if-trigger-present scan ==="
echo "proxy:        $PROXY_BASE_URL"
echo "triggers:     $(jq -r 'join(", ")' <<<"$TRIG_JSON")"
echo "model to add: $ADD_MODEL"
echo "scanned:      $(jq 'length' <<<"$ORGS") orgs, $(jq 'length' <<<"$TEAMS") teams, $(jq 'length' <<<"$KEYS") keys"
echo "mode:         $([[ $APPLY == 1 ]] && echo APPLY || echo DRY-RUN)"
echo

if [[ "$PLAN_COUNT" == 0 ]]; then
  echo "RESULT: no qualifying objects. Nothing to add."
  exit 0
fi

echo "found $PLAN_COUNT object(s) qualifying for the add:"
while IFS= read -r p; do
  [[ -z "$p" ]] && continue
  jq -r '"  [\(.level)] \(.name): \(.models | length) models -> \(.models | length + 1)"' <<<"$p"
done <<<"$PLANS"
echo

if [[ $APPLY != 1 ]]; then
  echo "dry-run only. Re-run with --apply to add $ADD_MODEL to the lists above."
  exit 0
fi

# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
while IFS= read -r p; do
  [[ -z "$p" ]] && continue
  level=$(jq -r '.level' <<<"$p")
  id=$(jq -r '.id' <<<"$p")
  name=$(jq -r '.name' <<<"$p")
  models=$(jq -c --arg a "$ADD_MODEL" '.models + [$a]' <<<"$p")
  case "$level" in
    orgs)  http PATCH /organization/update "{\"organization_id\":\"$id\",\"models\":$models}" >/dev/null ;;
    teams) http POST  /team/update        "{\"team_id\":\"$id\",\"models\":$models}"        >/dev/null ;;
    keys)  http POST  /key/update         "{\"key\":\"$id\",\"models\":$models}"           >/dev/null ;;
  esac
  echo "updated [$level] $name"
done <<<"$PLANS"

# ---------------------------------------------------------------------------
# Verify: re-fetch everything and confirm every qualifying object got the add
# ---------------------------------------------------------------------------
echo
echo "=== verification ==="
ORGS=$(http GET /organization/list)
TEAMS=$(http GET /team/list)
KEYS=$(fetch_keys)

PLANS=$(collect_plans | grep . || true)
PLAN_COUNT=$(wc -l <<<"$PLANS" | tr -d ' ')
[[ -z "$PLANS" ]] && PLAN_COUNT=0
if [[ "$PLAN_COUNT" == 0 ]]; then
  echo "RESULT: verified. Every qualifying object now includes $ADD_MODEL."
  exit 0
fi
echo "RESULT: $PLAN_COUNT object(s) still missing $ADD_MODEL:" >&2
while IFS= read -r p; do
  [[ -z "$p" ]] && continue
  jq -c . <<<"$p" >&2
done <<<"$PLANS"
exit 1
