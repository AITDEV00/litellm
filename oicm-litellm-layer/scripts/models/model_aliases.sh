#!/usr/bin/env bash
# List, add, or remove global aliases for a registered LiteLLM model group.
#
# Targets are registered model_name values, not litellm_params.model upstream
# IDs or deployment UUIDs. These are router aliases, not key/team aliases.
# Existing aliases are preserved. Writes require a DB-backed alias map and a
# proxy-admin key; config-owned maps are rejected. No live changes without
# --apply. Alias-edit writers should not run concurrently because the Admin API
# merges the read alias map into its persisted map as a whole.
#
# Requires: bash >=4, curl, jq >=1.6
# Environment: PROXY_BASE_URL (default https://litellm.ecouncil.ae),
# LITELLM_API_KEY. TLS verification is enabled; GATEWAY_INSECURE_TLS=1 opts out.
#
# Usage:
#   ./model_aliases.sh list
#   ./model_aliases.sh add Qwen3.6-35B-A3B-FP8 Qwen/Qwen3.6-35B-A3B-FP8
#   ./model_aliases.sh add Qwen3.6-35B-A3B-FP8 Qwen/Qwen3.6-35B-A3B-FP8 --apply
#   ./model_aliases.sh add short registered-model --hidden --apply
#   ./model_aliases.sh remove short --apply

set -euo pipefail

PROXY_BASE_URL="${PROXY_BASE_URL:-https://litellm.ecouncil.ae}"
PROXY_BASE_URL="${PROXY_BASE_URL%/}"
LITELLM_VERIFY_DELAY_SECONDS="${LITELLM_VERIFY_DELAY_SECONDS:-2}"
LITELLM_VERIFY_ATTEMPTS="${LITELLM_VERIFY_ATTEMPTS:-10}"
TMP_FILES=()
validate_verification_settings() {
  if ! [[ "$LITELLM_VERIFY_DELAY_SECONDS" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! jq -en "$LITELLM_VERIFY_DELAY_SECONDS | isfinite and . >= 0" >/dev/null; then
    echo 'ERROR: LITELLM_VERIFY_DELAY_SECONDS must be a finite non-negative number' >&2
    return 1
  fi
  if ! [[ "$LITELLM_VERIFY_ATTEMPTS" =~ ^[1-9][0-9]*$ ]]; then
    echo 'ERROR: LITELLM_VERIFY_ATTEMPTS must be a positive integer' >&2
    return 1
  fi
}
cleanup() {
  if (( ${#TMP_FILES[@]} > 0 )); then rm -f -- "${TMP_FILES[@]}"; fi
}
trap cleanup EXIT
CURL_BIN="$(command -v curl || true)"
if [[ -x /usr/bin/curl ]]; then CURL_BIN=/usr/bin/curl; fi
JQ_BIN="$(type -P jq || true)"
if [[ -z "$CURL_BIN" || -z "$JQ_BIN" ]]; then
  echo 'ERROR: curl and jq are required' >&2
  exit 1
fi
CURL=("$CURL_BIN" -sS --connect-timeout "${GATEWAY_CONNECT_TIMEOUT_SECONDS:-10}" --max-time "${GATEWAY_TIMEOUT_SECONDS:-30}")
case "${GATEWAY_INSECURE_TLS:-}" in
  1|true|TRUE|yes|YES) CURL+=(-k) ;;
esac
CURL+=(-H "Authorization: Bearer ${LITELLM_API_KEY:-}" -H "Content-Type: application/json")
CURL+=(--noproxy "${NO_PROXY:-${no_proxy:-*}}")

usage() { sed -n 's/^# \{0,1\}//p' "$0" | sed '1{/^!/d;}'; exit "${1:-0}"; }
jq() { "$JQ_BIN" "$@"; }
die() { echo "ERROR: $*" >&2; return 1; }

http() {
  local method="$1" path="$2" body="${3:-}" out code curl_status
  local request_file=""
  if [[ -n "$body" ]]; then
    request_file=$(mktemp "${TMPDIR:-/tmp}/model-aliases.XXXXXX")
    TMP_FILES+=("$request_file")
    printf '%s' "$body" >"$request_file"
    body="@$request_file"
  else
    body="@/dev/null"
  fi
  set +e
  out=$("${CURL[@]}" -X "$method" "$PROXY_BASE_URL$path" -d "$body" -w $'\n%{http_code}')
  curl_status=$?
  set -e
  if (( curl_status != 0 )); then
    die "$method $path failed: curl exit $curl_status"
    return 1
  fi
  code="${out##*$'\n'}"
  out="${out%$'\n'*}"
  if [[ "$code" != 2* ]]; then
    die "$method $path -> HTTP $code: ${out:0:1000}"
    return 1
  fi
  printf '%s' "$out"
}

read_aliases() {
  http GET /router/settings \
    | jq -ce '(.current_values.model_group_alias // {}) | if type == "object" then . else error("alias map is not an object") end'
}

list_aliases() {
  read_aliases | jq .
}

edit_aliases() {
  local operation="$1" alias="$2" target="${3:-}" hidden="${4:-false}" apply="${5:-false}"
  local settings current registered existing_target=""
  if [[ -z "$alias" || "$alias" =~ ^[[:space:]] || "$alias" =~ [[:space:]]$ ]]; then
    die "alias must be nonempty with no leading/trailing whitespace"
    return 1
  fi

  settings=$(http GET /router/settings) || return 1
  if [[ $(jq -r '.source.model_group_alias // ""' <<<"$settings") == config ]]; then
    die "model_group_alias is owned by config.yaml; edit the gateway config"
    return 1
  fi
  current=$(jq -ce '(.current_values.model_group_alias // {}) | if type == "object" then . else error("alias map is not an object") end' <<<"$settings") || return 1

  registered=$(http GET /model/info \
    | jq -ce '[.data[]? | select(type == "object" and (.model_name | type == "string")) | .model_name] | unique') || return 1
  if jq -e --arg alias "$alias" 'any(.[]; . == $alias)' <<<"$registered" >/dev/null; then
    die "'$alias' is already a registered model; refusing to shadow it"
    return 1
  fi

  if [[ "$operation" == add ]]; then
    if [[ "$target" == "$alias" ]]; then
      die "alias and target must differ"
      return 1
    fi
    if ! jq -e --arg target "$target" 'any(.[]; . == $target)' <<<"$registered" >/dev/null; then
      die "target '$target' is not a registered model; use a model_name from /model/info"
      return 1
    fi
    if jq -e --arg target "$target" '(.[$target] | type) == "string" or (.[$target].model | type) == "string"' <<<"$current" >/dev/null; then
      die "target '$target' is itself an alias; use the underlying registered model"
      return 1
    fi
    existing_target=$(jq -r --arg alias "$alias" '
      if .[$alias] == null then empty
      elif .[$alias] | type == "string" then .[$alias]
      elif .[$alias].model | type == "string" then .[$alias].model
      else error("existing alias is malformed; repair it through the Admin API explicitly") end' <<<"$current") || return 1
  fi

  if [[ "$operation" == add && -n "$existing_target" && "$existing_target" != "$target" ]]; then
    die "alias '$alias' already targets '$existing_target', not '$target'"
    return 1
  fi
  if [[ "$operation" == add && "$existing_target" == "$target" ]]; then
    if [[ "$hidden" == false ]] || jq -e --arg alias "$alias" --arg target "$target" \
      '.[$alias] == {model: $target, hidden: true}' <<<"$current" >/dev/null; then
      echo "No change: alias '$alias' already has this target and visibility"
      return 0
    fi
  fi

  if [[ "$operation" == remove ]]; then
    if ! jq -e --arg alias "$alias" 'has($alias)' <<<"$current" >/dev/null; then
      echo "No change: alias '$alias' does not exist"
      return 0
    fi
    current=$(jq -ce --arg alias "$alias" 'del(.[$alias])' <<<"$current")
  elif [[ "$hidden" == true ]]; then
    current=$(jq -ce --arg alias "$alias" --arg target "$target" '
      .[$alias] = {model: $target, hidden: true}' <<<"$current")
  else
    current=$(jq -ce --arg alias "$alias" --arg target "$target" '.[$alias] = $target' <<<"$current")
  fi

  local body
  if ! body=$(jq -cn --argjson aliases "$current" '{router_settings: {model_group_alias: $aliases}}'); then
    die "failed to build the alias update payload"
    return 1
  fi
  if [[ "$apply" != true ]]; then
    echo "DRY-RUN; re-run with --apply to write:"
    jq . <<<"$body"
    return 0
  fi

  http POST /config/update "$body" >/dev/null || return 1
  local persisted attempts=0
  while (( attempts < LITELLM_VERIFY_ATTEMPTS )); do
    attempts=$((attempts + 1))
    persisted=$(read_aliases) || return 1
    if jq -e --argjson expected "$current" '. == $expected' <<<"$persisted" >/dev/null; then
      echo "Applied and verified alias '$alias' after $attempts read(s):"
      jq . <<<"$persisted"
      return 0
    fi
    if (( attempts < LITELLM_VERIFY_ATTEMPTS )); then sleep "$LITELLM_VERIFY_DELAY_SECONDS"; fi
  done
  die "gateway returned a different alias map during verification after $attempts reads with ${LITELLM_VERIFY_DELAY_SECONDS}s delays; inspect /router/settings before retrying"
  return 1
}

main() {
  if (( $# == 0 )); then usage 1; fi
  case "${1:-}" in -h|--help) usage 0 ;; esac
  if ! command -v curl >/dev/null || ! command -v jq >/dev/null; then
    die "curl and jq are required"
    return 1
  fi
  if [[ ! "${GATEWAY_TIMEOUT_SECONDS:-30}" =~ ^[0-9]+([.][0-9]+)?$ || ! "${GATEWAY_CONNECT_TIMEOUT_SECONDS:-10}" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    die "timeout settings must be non-negative numbers"
    return 1
  fi
  if [[ -z "${LITELLM_API_KEY:-}" ]]; then
    die "set LITELLM_API_KEY to a proxy-admin key"
    return 1
  fi
  validate_verification_settings || return 1

  local operation="$1" apply=false hidden=false positional=()
  shift
  while (( $# > 0 )); do
    case "$1" in
      --apply) apply=true ;;
      --hidden) hidden=true ;;
      -h|--help) usage 0 ;;
      --) die "unexpected argument '$1'" ;;
      -*) die "unknown option '$1'" ;;
      *) positional+=("$1") ;;
    esac
    shift
  done
  case "$operation" in
    list) (( ${#positional[@]} == 0 )) || { usage 1; }; list_aliases ;;
    add)
      (( ${#positional[@]} == 2 )) || { echo "Usage: $0 add <alias> <registered-model-name> [--hidden] [--apply]" >&2; return 1; }
      edit_aliases add "${positional[0]}" "${positional[1]}" "$hidden" "$apply"
      ;;
    remove)
      (( ${#positional[@]} == 1 )) || { echo "Usage: $0 remove <alias> [--apply]" >&2; return 1; }
      [[ "$hidden" == false ]] || die "--hidden is only valid for add"
      edit_aliases remove "${positional[0]}" "" false "$apply"
      ;;
    *) die "unknown command '$operation'; expected list, add, or remove" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
