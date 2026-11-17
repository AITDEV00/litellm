#!/usr/bin/env bash
# Create an unmanaged, DB-backed model entry that routes to an existing
# deployment's upstream while carrying its own request parameters.
#
# This is the script form of docs/admin-api/LITELLM-ADMIN-REST-API.md
# "Creating a persistent duplicate". The new row has no `oicm_uuid`, so the
# OICM discovery controller never sees it, never patches it, and never deletes
# it. It is still written to the database (`db_model: true`), so the proxy's
# 30-second `add_deployment` job keeps it in the router across restarts.
#
# Use it when a second public name must send different parameters to the same
# vLLM server. A global alias cannot do that: an alias is a name-to-name
# pointer with no parameters of its own.
#
# The new row is a separate deployment ID. It has its own health state,
# cooldown, RPM/TPM counters, and its own `model_group` bucket in usage, even
# though it prices against the upstream you copy from.
#
# DRY RUN by default. Pass --apply to write.
#
# Usage:
#   export PROXY_BASE_URL="https://litellm.ecouncil.ae"
#   export LITELLM_API_KEY="sk-..."
#
#   # Copy an existing deployment's model/api_base/pricing, add extra params
#   ./create_model_entry.sh NEW-NAME --from Qwen/Qwen3.8-Flash-Next-FP8 \
#     --params '{"reasoning_effort":"none"}'
#
#   # Same, written to the proxy
#   ./create_model_entry.sh NEW-NAME --from Qwen/Qwen3.8-Flash-Next-FP8 \
#     --params '{"reasoning_effort":"none"}' --apply
#
# Positional:
#   1  model_name for the new entry (must not already be registered)
#
# Options:
#   --from NAME|ID        existing deployment to copy model/api_base/pricing from
#   --params JSON         extra litellm_params merged over the copied ones
#   --param K=V           single extra param, repeatable; values parse as JSON
#                         when possible, else as a string
#   --api-base URL        override the copied api_base
#
# Requires: curl, jq (>=1.6)

set -euo pipefail

PROXY_BASE_URL="${PROXY_BASE_URL:-https://litellm.ecouncil.ae}"
PROXY_BASE_URL="${PROXY_BASE_URL%/}"
LITELLM_VERIFY_ATTEMPTS="${LITELLM_VERIFY_ATTEMPTS:-10}"
LITELLM_VERIFY_DELAY_SECONDS="${LITELLM_VERIFY_DELAY_SECONDS:-2}"

# litellm_params the script owns. Refused in --params because overriding them
# silently changes what the entry points at rather than how it behaves.
PROTECTED_PARAMS="model api_base api_key custom_llm_provider api_version
organization model_client_overrides use_in_pass_through
max_parallel_requests timeout num_retries weight"

usage() { sed -n 's/^# \{0,1\}//p' "$0" | sed '1{/^!/d;}'; exit "${1:-0}"; }
die() { echo "ERROR: $*" >&2; return 1; }

JQ_BIN="$(type -P jq || true)"
if [[ -z "$(command -v curl || true)" || -z "$JQ_BIN" ]]; then
  die "curl and jq are required"
  exit 1
fi
jq() { "$JQ_BIN" "$@"; }

CURL=("$(command -v curl)" -sS --connect-timeout "${GATEWAY_CONNECT_TIMEOUT_SECONDS:-10}" --max-time "${GATEWAY_TIMEOUT_SECONDS:-30}")
case "${GATEWAY_INSECURE_TLS:-}" in
  1|true|TRUE|yes|YES) CURL+=(-k) ;;
esac
CURL+=(-H "Authorization: Bearer ${LITELLM_API_KEY:-}" -H "Content-Type: application/json")
CURL+=(--noproxy "${NO_PROXY:-${no_proxy:-*}}")

http() {
  local method="$1" path="$2" body="${3:-}"
  local out code curl_status request_file=""
  if [[ -n "$body" ]]; then
    request_file=$(mktemp "${TMPDIR:-/tmp}/create-model-entry.XXXXXX")
    printf '%s' "$body" >"$request_file"
    body="@$request_file"
  fi
  set +e
  if [[ -n "$body" ]]; then
    out=$("${CURL[@]}" -X "$method" "$PROXY_BASE_URL$path" -d "$body" -w $'\n%{http_code}')
  else
    out=$("${CURL[@]}" -X "$method" "$PROXY_BASE_URL$path" -w $'\n%{http_code}')
  fi
  curl_status=$?
  set -e
  if [[ -n "$request_file" ]]; then rm -f -- "$request_file"; fi
  (( curl_status == 0 )) || { die "$method $path failed: curl exit $curl_status"; return 1; }
  code="${out##*$'\n'}"
  out="${out%$'\n'*}"
  [[ "$code" == 2* ]] || { die "$method $path -> HTTP $code: ${out:0:1000}"; return 1; }
  printf '%s' "$out"
}

read_entries() { http GET "/v2/model/info?size=1000"; }

# A model_name is already taken by a regular deployment, an alias, or an
# unmanaged entry. Creating another row under the same name would load-balance
# one public name across two different parameter sets.
assert_name_free() {
  local name="$1" entries="$2"
  if jq -e --arg n "$name" 'any(.data[]?; .model_name == $n)' <<<"$entries" >/dev/null; then
    die "'$name' is already registered; pick a different model_name"
    return 1
  fi
}

resolve_source() {
  local target="$1" entries="$2" matches count
  matches=$(jq -c --arg t "$target" '
    [ .data[]
      | select(.model_info.id == $t or .model_name == $t)
      | { id: .model_info.id, name: .model_name, lp: (.litellm_params // {}) } ]' <<<"$entries")
  count=$(jq 'length' <<<"$matches")
  if [[ "$count" == 0 ]]; then
    die "no deployment matches --from '$target' by model_name or model_info.id"
    return 1
  fi
  if [[ "$count" != 1 ]]; then
    echo "ERROR: --from '$target' is ambiguous, $count deployments match:" >&2
    jq -r '.[] | "  \(.id)  \(.name)"' <<<"$matches" >&2
    return 1
  fi
  printf '%s' "$(jq -c '.[0]' <<<"$matches")"
}

for p in $PROTECTED_PARAMS; do :; done
is_protected() {  local key="$1" candidate
  for candidate in $PROTECTED_PARAMS; do
    [[ "$key" == "$candidate" ]] && return 0
  done
  return 1
}

main() {
  if (( $# == 0 )); then usage 1; fi
  case "${1:-}" in -h|--help) usage 0 ;; esac
  [[ -n "${LITELLM_API_KEY:-}" ]] || { die "set LITELLM_API_KEY to a proxy-admin key"; return 1; }

  local name="" from="" params_json="{}" api_base="" apply=false
  local -a single_params=()
  name="$1"; shift
  while (( $# > 0 )); do
    case "$1" in
      --from) from="${2:?--from needs a model name or id}"; shift 2 ;;
      --params) params_json="${2:?--params needs a JSON object}"; shift 2 ;;
      --param) single_params+=("${2:?--param needs KEY=VALUE}"); shift 2 ;;
      --api-base) api_base="${2:?--api-base needs a URL}"; shift 2 ;;
      --apply) apply=true; shift ;;
      -h|--help) usage 0 ;;
      *) die "unknown argument '$1'"; return 1 ;;
    esac
  done

  [[ -n "$from" ]] || { die "--from <model-name-or-id> is required"; return 1; }
  jq -e 'type == "object"' <<<"$params_json" >/dev/null 2>&1 || {
    die "--params must be a JSON object, got: $params_json"; return 1; }

  local entry key value
  for entry in "${single_params[@]}"; do
    key="${entry%%=*}"
    value="${entry#*=}"
    [[ "$key" != "$entry" && -n "$key" ]] || { die "--param must be KEY=VALUE, got '$entry'"; return 1; }
    if [[ "$value" =~ ^(\{.*\}|\[.*\]|true|false|null|-?[0-9]+(\.[0-9]+)?)$ ]]; then
      params_json=$(jq -c --arg k "$key" --argjson v "$value" '.[$k] = $v' <<<"$params_json") || {
        die "--param $key: '$value' looks like JSON but does not parse"; return 1; }
    else
      params_json=$(jq -c --arg k "$key" --arg v "$value" '.[$k] = $v' <<<"$params_json")
    fi
  done

  local bad_keys
  bad_keys=$(jq -r --arg list "$PROTECTED_PARAMS" \
    '[$list | splits(" ") | select(length > 0)] as $protected
     | [keys[] | select(. as $k | $protected | index($k))] | join(", ")' <<<"$params_json")
  [[ -z "$bad_keys" ]] || {
    die "--params may not set routing/credential fields: $bad_keys. Use --api-base for the endpoint."
    return 1; }

  local entries source new_params body
  entries=$(read_entries) || return 1
  assert_name_free "$name" "$entries" || return 1
  source=$(resolve_source "$from" "$entries") || return 1

  new_params=$(jq -c --argjson extra "$params_json" --arg base "$api_base" '
    (.lp
      | { model, api_base, drop_params: (.drop_params // true) }
      + (if .input_cost_per_token then { input_cost_per_token } else {} end)
      + (if .output_cost_per_token then { output_cost_per_token } else {} end))
    + (if $base != "" then { api_base: $base } else {} end)
    + $extra' <<<"$source")

  # An empty api_key keeps the OpenAI-compatible client keyless, which the
  # hosted_vllm path resolves from the deployment itself.
  new_params=$(jq -c 'if has("api_key") then . else . + {api_key: ""} end' <<<"$new_params")

  body=$(jq -c --arg n "$name" --argjson lp "$new_params" \
    '{model_name: $n, litellm_params: $lp, model_info: {db_model: true}}' <<<"{}")

  echo "=== create model entry ==="
  echo "proxy:     $PROXY_BASE_URL"
  echo "new name:  $name"
  echo "source:    $(jq -r '.name + " (" + .id + ")"' <<<"$source")"
  echo "upstream:  $(jq -r '.model' <<<"$new_params")"
  echo "api_base:  $(jq -r '.api_base' <<<"$new_params")"
  echo "extra:     $(jq -c . <<<"$params_json")"
  echo "mode:      $($apply && echo APPLY || echo DRY-RUN)"
  echo
  echo "$body" | jq '{model_name, model_info, litellm_params}'

  if [[ "$apply" != true ]]; then
    echo
    echo "dry-run only. Re-run with --apply to create '$name'."
    return 0
  fi

  http POST /model/new "$body" >/dev/null || return 1

  # A create is only durable if the row is in the DB: the 30-second
  # add_deployment job evicts any router deployment the DB does not list.
  local created_id="" attempt=0 observed
  while (( attempt < LITELLM_VERIFY_ATTEMPTS )); do
    attempt=$((attempt + 1))
    observed=$(read_entries) || return 1
    created_id=$(jq -r --arg n "$name" '[.data[] | select(.model_name == $n)] | if length == 1 then .[0].model_info.id else "" end' <<<"$observed")
    [[ -n "$created_id" ]] && break
    sleep "$LITELLM_VERIFY_DELAY_SECONDS"
  done
  [[ -n "$created_id" ]] || { die "'$name' did not appear in /v2/model/info after $attempt reads"; return 1; }

  local persisted extra_ok
  persisted=$(read_entries | jq -c --arg i "$created_id" '.data[] | select(.model_info.id == $i)')
  if ! jq -e '(.model_info.db_model == true) and ((.model_info.oicm_uuid // null) == null)' <<<"$persisted" >/dev/null; then
    die "created row is not controller-safe: need db_model=true and no oicm_uuid, got $(jq -c '.model_info' <<<"$persisted")"
    return 1
  fi

  extra_ok=$(jq -c --argjson extra "$params_json" --argjson stored_lp "$(jq -c '.litellm_params' <<<"$persisted")" '
    [ $extra | to_entries[] | select(.value != null) | select(.value != $stored_lp[.key]) ]' <<<"{}")
  local missing
  missing=$(jq 'length' <<<"$extra_ok")
  if [[ "$missing" != 0 ]]; then
    echo "WARNING: $missing extra param(s) did not round-trip: $(jq -c . <<<"$extra_ok")" >&2
  fi

  echo
  echo "created '$name' -> $created_id"
  echo "verify:  db_model=$(jq -r '.model_info.db_model' <<<"$persisted") oicm_uuid=$(jq -r '.model_info.oicm_uuid // "NONE"' <<<"$persisted")"
  echo "next:    confirm a controller sync (SYNC_INTERVAL) logs no Deleted/re-registration for this id"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
