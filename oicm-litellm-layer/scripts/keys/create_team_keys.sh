#!/usr/bin/env bash
# Create N virtual keys in a team, named "<prefix>-1", "<prefix>-2", ...
#
# Aliases are globally unique across the proxy, not per team, so the script
# continues from the highest suffix already taken for the prefix instead of
# restarting at 1 and colliding with an existing key.
#
# DRY RUN by default. Pass --apply to create the keys.
#
# Usage:
#   export LITELLM_API_KEY="sk-..."
#   ./create_team_keys.sh --prefix testing-opencode --count 5
#   ./create_team_keys.sh --prefix testing-opencode --count 5 --apply
#   ./create_team_keys.sh --prefix testing-opencode --count 5 --apply --out opencode-keys.tsv
#
# Options:
#   --prefix P    alias prefix; keys are named P-1, P-2, ... (required)
#   --count N     how many keys to create (required)
#   --team T      team alias or team_id (default: DEVELOPER)
#   --priority P  metadata.priority, must exist in litellm_settings.priority_reservation
#                 (default: prior3)
#   --out F       append "alias<TAB>key" lines to F, created mode 0600
#   --apply       create the keys; without it only the plan is printed
#   -h, --help    show this help
#
# Environment:
#   PROXY_BASE_URL  default: https://litellm.ecouncil.ae
#   LITELLM_API_KEY required; needs proxy_admin (the master key works)
#
# Requires: curl, jq (>=1.6)

set -euo pipefail

PROXY_BASE_URL="${PROXY_BASE_URL:-https://litellm.ecouncil.ae}"
APPLY=0
PREFIX=""
COUNT=""
TEAM="DEVELOPER"
PRIORITY="prior3"
OUT=""

usage() {
  cat <<'EOF'
Create N virtual keys in a team, named "<prefix>-1", "<prefix>-2", ...

Aliases are globally unique across the proxy, not per team, so the script
continues from the highest suffix already taken for the prefix instead of
restarting at 1 and colliding with an existing key.

DRY RUN by default. Pass --apply to create the keys.

Usage:
  export LITELLM_API_KEY="sk-..."
  ./create_team_keys.sh --prefix testing-opencode --count 5
  ./create_team_keys.sh --prefix testing-opencode --count 5 --apply
  ./create_team_keys.sh --prefix testing-opencode --count 5 --apply --out opencode-keys.tsv

Options:
  --prefix P    alias prefix; keys are named P-1, P-2, ... (required)
  --count N     how many keys to create (required)
  --team T      team alias or team_id (default: DEVELOPER)
  --priority P  metadata.priority, must exist in litellm_settings.priority_reservation
                (default: prior3)
  --out F       append "alias<TAB>key" lines to F, created mode 0600
  --apply       create the keys; without it only the plan is printed
  -h, --help    show this help

Environment:
  PROXY_BASE_URL  default: https://litellm.ecouncil.ae
  LITELLM_API_KEY required; needs proxy_admin (the master key works)

Requires: curl, jq (>=1.6)
EOF
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix)   PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
    --count)    COUNT="${2:?--count needs a value}"; shift 2 ;;
    --team)     TEAM="${2:?--team needs a value}"; shift 2 ;;
    --priority) PRIORITY="${2:?--priority needs a value}"; shift 2 ;;
    --out)      OUT="${2:?--out needs a value}"; shift 2 ;;
    --apply)    APPLY=1; shift ;;
    -h|--help)  usage 0 ;;
    *) echo "unknown argument: $1" >&2; usage 1 ;;
  esac
done

[[ -n "$PREFIX" ]] || { echo "--prefix is required" >&2; usage 1; }
[[ -n "$COUNT" ]] || { echo "--count is required" >&2; usage 1; }
[[ "$COUNT" =~ ^[1-9][0-9]*$ ]] || { echo "--count must be a positive integer, got '$COUNT'" >&2; exit 1; }
[[ "$PREFIX" != *[[:space:]]* ]] || { echo "--prefix must not contain whitespace" >&2; exit 1; }
[[ -n "$PRIORITY" ]] || { echo "--priority must not be empty" >&2; exit 1; }

: "${LITELLM_API_KEY:?set LITELLM_API_KEY to a proxy_admin key}"

command -v curl >/dev/null && command -v jq >/dev/null || { echo "curl and jq are required" >&2; exit 1; }

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

MATCHES=$(http GET /team/list | jq -c --arg t "$TEAM" \
  '[.[] | select(.team_alias == $t or .team_id == $t) | .team_id] | unique')
N_MATCHES=$(jq 'length' <<<"$MATCHES")
if (( N_MATCHES != 1 )); then
  echo "team '$TEAM' matched $N_MATCHES teams; expected exactly 1" >&2
  jq -r '.[]' <<<"$MATCHES" >&2
  exit 1
fi
TEAM_ID=$(jq -r '.[0]' <<<"$MATCHES")

MAX_SUFFIX=0
PAGE=1
while :; do
  QUERY=$(jq -rn --arg p "$PREFIX" '$p | @uri')
  CHUNK=$(http GET "/key/aliases?search=$QUERY&page=$PAGE&size=100")
  TOTAL_PAGES=$(jq '.total_pages // 1' <<<"$CHUNK")
  while IFS= read -r existing; do
    if [[ "$existing" == "$PREFIX-"* ]]; then
      suffix="${existing#"$PREFIX-"}"
      if [[ "$suffix" =~ ^[0-9]+$ ]] && (( 10#$suffix > MAX_SUFFIX )); then
        MAX_SUFFIX=$(( 10#$suffix ))
      fi
    fi
  done < <(jq -r '.aliases[]?' <<<"$CHUNK")
  if (( PAGE >= TOTAL_PAGES )); then
    break
  fi
  PAGE=$(( PAGE + 1 ))
done
NEXT=$(( MAX_SUFFIX + 1 ))

echo "team     : $TEAM ($TEAM_ID)"
echo "priority : metadata.priority=$PRIORITY"
echo "aliases  : $PREFIX-$NEXT .. $PREFIX-$(( NEXT + COUNT - 1 ))"
echo

if (( APPLY == 0 )); then
  echo "DRY RUN: nothing created. Re-run with --apply to create $COUNT key(s)."
  exit 0
fi

if [[ -n "$OUT" ]]; then
  touch "$OUT"
  chmod 600 "$OUT"
fi

printf 'ALIAS\tKEY\n'
CREATED=0
for (( i = NEXT; i < NEXT + COUNT; i++ )); do
  alias="$PREFIX-$i"
  body=$(jq -n --arg a "$alias" --arg t "$TEAM_ID" --arg p "$PRIORITY" \
    '{key_alias: $a, team_id: $t, metadata: {priority: $p}}')
  resp=$(http POST /key/generate "$body")
  key=$(jq -r '.key // empty' <<<"$resp")
  if [[ -z "$key" ]]; then
    echo "ERROR: /key/generate returned no key for alias '$alias': ${resp:0:300}" >&2
    exit 1
  fi
  printf '%s\t%s\n' "$alias" "$key"
  if [[ -n "$OUT" ]]; then
    printf '%s\t%s\n' "$alias" "$key" >> "$OUT"
  fi
  CREATED=$(( CREATED + 1 ))
done

echo
echo "created $CREATED key(s) in team $TEAM ($TEAM_ID) with metadata.priority=$PRIORITY"
if [[ -n "$OUT" ]]; then
  echo "appended to $OUT"
fi
