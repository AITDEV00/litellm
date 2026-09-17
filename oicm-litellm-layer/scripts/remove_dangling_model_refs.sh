#!/usr/bin/env bash
# Remove dangling model-id references (models referenced by orgs/teams/keys but
# no longer registered on the proxy) from organization, team, and API-key model
# access lists. Exact-match only: removing "zai-org/GLM-5.3" never touches
# "zai-org/GLM-5.3-Flash".
#
# DRY RUN by default. Pass --apply to write changes.
#
# Usage:
#   export PROXY_BASE_URL="https://litellm.ecouncil.ae"
#   export LITELLM_API_KEY="sk-..."
#   ./remove_dangling_model_refs.sh                 # dry-run report
#   ./remove_dangling_model_refs.sh --apply         # fix + verify
#   ./remove_dangling_model_refs.sh --only teams    # limit scope: orgs|teams|keys
#   ./remove_dangling_model_refs.sh --apply --only keys
#
# Requires: curl, jq (>=1.6)

set -euo pipefail

APPLY=0
ONLY="all"

usage() { grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --only)  ONLY="${2:?scope required: orgs|teams|keys}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

: "${PROXY_BASE_URL:?set PROXY_BASE_URL}"
: "${LITELLM_API_KEY:?set LITELLM_API_KEY}"

command -v curl >/dev/null && command -v jq >/dev/null || { echo "curl and jq are required" >&2; exit 1; }

case "$ONLY" in all|orgs|teams|keys) ;; *) echo "invalid --only: $ONLY" >&2; exit 1 ;; esac

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
# Dangling detection. An entry is dangling when it is referenced by an access
# list but absent from /model/info. Wildcards ("x/*") and LiteLLM sentinels
# (all, all-team-models, all-proxy-models, no-default-models) are never
# considered dangling.
# ---------------------------------------------------------------------------
SENTINELS='["all","all-team-models","all-proxy-models","no-default-models"]'

JQ_FILTER='
  def is_dangling($x):
    ($x | contains("*") | not) and ($S | index($x) | not) and ($V | index($x) | not);
  .[]
  | (.models // []) as $ms
  | [ $ms[] | select(is_dangling(.)) ] as $dang
  | select($dang | length > 0)
  | { level: $level,
      id: .[$idfield],
      name: .[$namefield],
      dangling: $dang,
      new_models: [ $ms[] | select(is_dangling(.) | not) ] }
'

plan_for() {
  local level="$1" json="$2" idfield="$3" namefield="$4"
  jq -c --argjson V "$VALID" --argjson S "$SENTINELS" \
    --arg level "$level" --arg idfield "$idfield" --arg namefield "$namefield" \
    "$JQ_FILTER" <<<"$json" || { echo "ERROR: dangling-scan filter failed for $level" >&2; exit 1; }
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

# One JSON object per line, or nothing when no dangling refs were found.
PLANS=$(collect_plans | grep . || true)
PLAN_COUNT=$(wc -l <<<"$PLANS" | tr -d ' ')
[[ -z "$PLANS" ]] && PLAN_COUNT=0

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
echo "=== dangling model reference scan ==="
echo "proxy:       $PROXY_BASE_URL"
echo "valid models on proxy: $(jq 'length' <<<"$VALID")"
echo "scanned:     $(jq 'length' <<<"$ORGS") orgs, $(jq 'length' <<<"$TEAMS") teams, $(jq 'length' <<<"$KEYS") keys"
echo "mode:        $([[ $APPLY == 1 ]] && echo APPLY || echo DRY-RUN)"
echo

if [[ "$PLAN_COUNT" == 0 ]]; then
  echo "RESULT: clean. No dangling model references found."
  exit 0
fi

echo "found $PLAN_COUNT object(s) with dangling references:"
while IFS= read -r p; do
  [[ -z "$p" ]] && continue
  jq -r '"  [\(.level)] \(.name): removing \(.dangling | join(", "))  (\(.new_models | length) models remain)"' <<<"$p"
done <<<"$PLANS"
echo

if [[ $APPLY != 1 ]]; then
  echo "dry-run only. Re-run with --apply to remove the references above."
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
  models=$(jq -c '.new_models' <<<"$p")
  case "$level" in
    orgs)  http PATCH /organization/update "{\"organization_id\":\"$id\",\"models\":$models}" >/dev/null ;;
    teams) http POST  /team/update        "{\"team_id\":\"$id\",\"models\":$models}"        >/dev/null ;;
    keys)  http POST  /key/update         "{\"key\":\"$id\",\"models\":$models}"           >/dev/null ;;
  esac
  echo "updated [$level] $name"
done <<<"$PLANS"

# ---------------------------------------------------------------------------
# Verify: re-fetch everything and confirm nothing dangling remains
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
  echo "RESULT: verified clean. No dangling model references remain."
  exit 0
fi
echo "RESULT: $PLAN_COUNT object(s) still contain dangling references:" >&2
while IFS= read -r p; do
  [[ -z "$p" ]] && continue
  jq -c . <<<"$p" >&2
done <<<"$PLANS"
exit 1
