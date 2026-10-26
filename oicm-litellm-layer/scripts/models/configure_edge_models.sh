#!/usr/bin/env bash
#
# Registers the ADEO EDGE models on a local EDGE proxy, each pointed at the
# production gateway. The edge instance holds no model list of its own, so this
# is what makes it serve traffic: without it /v1/models is empty and every
# completion 400s with "model not found".
#
# Usage:
#   export EDGE_MASTER_KEY=$(python3 scripts/get_master_key.py)
#   export PROD_API_KEY=sk-...
#   ./scripts/models/configure_edge_models.sh
#
# Required:
#   EDGE_MASTER_KEY  master key of the EDGE proxy (admin, can write models)
#   PROD_API_KEY     production virtual key the edge uses as the upstream key
#
# Optional:
#   EDGE_BASE   default http://localhost:4000
#   PROD_BASE   default https://litellm.ecouncil.ae
#   DRY_RUN     set to 1 to print the payloads without posting
#
# The edge proxy must run with store_model_in_db: true. Without it
# POST /model/new returns 500 and nothing is registered.

set -euo pipefail

EDGE_BASE="${EDGE_BASE:-http://localhost:4000}"
PROD_BASE="${PROD_BASE:-https://litellm.ecouncil.ae}"
EDGE_MASTER_KEY="${EDGE_MASTER_KEY:-}"
PROD_API_KEY="${PROD_API_KEY:-}"
DRY_RUN="${DRY_RUN:-0}"

if [ -z "$EDGE_MASTER_KEY" ]; then
    echo "EDGE_MASTER_KEY is required (admin key of the edge proxy)" >&2
    exit 1
fi
if [ -z "$PROD_API_KEY" ]; then
    echo "PROD_API_KEY is required (production virtual key used upstream)" >&2
    exit 1
fi

# Validate the upstream key BEFORE touching the edge's model list. Writing first
# and verifying afterwards means a bad key overwrites working deployments and
# breaks live traffic, which is worse than doing nothing.
if [ "$DRY_RUN" != "1" ]; then
    probe_code="$(
        curl -sS -o /dev/null -w '%{http_code}' -m 30 "$PROD_BASE/v1/models" \
            -H "Authorization: Bearer $PROD_API_KEY"
    )"
    if [ "$probe_code" != "200" ]; then
        echo "PROD_API_KEY rejected by $PROD_BASE (HTTP $probe_code); not changing anything" >&2
        exit 1
    fi
fi

# alias|upstream model id. The upstream id is what production serves, which is
# not the same as the alias: production registers the vendor-namespaced name.
MODELS=(
    "glm-5.3|zai-org/GLM-5.3"
    "kimi-k3|moonshotai/Kimi-K3"
    "deepseek-v4.1-flash|deepseek-ai/DeepSeek-V4.1-Flash"
)

payload() {
    local alias="$1" upstream="$2"
    # model is prefixed with openai/ so LiteLLM speaks the OpenAI wire format to
    # the upstream, which is what the production gateway exposes.
    printf '{"model_name":"%s","litellm_params":{"model":"openai/%s","api_base":"%s/v1","api_key":"%s"}}' \
        "$alias" "$upstream" "$PROD_BASE" "$PROD_API_KEY"
}

# Existing model_id for an alias, or empty when absent. /model/new mints a new
# row every call, so without this a rerun silently doubles every deployment and
# the router starts load-balancing across identical copies.
existing_id() {
    local alias="$1"
    curl -sS "$EDGE_BASE/model/info" -H "Authorization: Bearer $EDGE_MASTER_KEY" \
        | ALIAS="$alias" python3 -c '
import json, os, sys
alias = os.environ["ALIAS"]
try:
    rows = json.load(sys.stdin).get("data", [])
except Exception:
    sys.exit(0)
for row in rows:
    if row.get("model_name") == alias:
        print(row.get("model_info", {}).get("id", ""))
        break
'
}

failures=0

for entry in "${MODELS[@]}"; do
    alias="${entry%%|*}"
    upstream="${entry##*|}"

    if [ "$DRY_RUN" = "1" ]; then
        echo "[dry-run] POST $EDGE_BASE/model/new"
        payload "$alias" "$upstream" | sed 's/"api_key":"[^"]*"/"api_key":"***"/'
        echo
        continue
    fi

    body="$(payload "$alias" "$upstream")"
    mid="$(existing_id "$alias")"

    if [ -n "$mid" ]; then
        code="$(
            curl -sS -o /tmp/edge_model_resp.$$ -w '%{http_code}' -X PATCH "$EDGE_BASE/model/$mid/update" \
                -H "Authorization: Bearer $EDGE_MASTER_KEY" \
                -H 'Content-Type: application/json' \
                -d "{\"litellm_params\":{\"model\":\"openai/$upstream\",\"api_base\":\"$PROD_BASE/v1\",\"api_key\":\"$PROD_API_KEY\"}}"
        )"
        action="updated"
    else
        code="$(
            curl -sS -o /tmp/edge_model_resp.$$ -w '%{http_code}' -X POST "$EDGE_BASE/model/new" \
                -H "Authorization: Bearer $EDGE_MASTER_KEY" \
                -H 'Content-Type: application/json' \
                -d "$body"
        )"
        action="registered"
    fi

    if [ "$code" = "200" ]; then
        echo "$action $alias -> $upstream"
    else
        echo "FAILED $alias (HTTP $code): $(head -c 300 /tmp/edge_model_resp.$$)" >&2
        failures=$((failures + 1))
    fi
    rm -f /tmp/edge_model_resp.$$
done

if [ "$DRY_RUN" = "1" ]; then
    exit 0
fi

echo
echo "Verifying each model round-trips through the edge to production"
for entry in "${MODELS[@]}"; do
    alias="${entry%%|*}"
    reply="$(
        curl -sS -m 90 -X POST "$EDGE_BASE/v1/chat/completions" \
            -H "Authorization: Bearer $EDGE_MASTER_KEY" \
            -H 'Content-Type: application/json' \
            -d "{\"model\":\"$alias\",\"messages\":[{\"role\":\"user\",\"content\":\"reply with exactly: OK\"}],\"max_tokens\":32}" \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); c=d.get("choices"); print("ok" if c else "error: " + json.dumps(d)[:200])'
    )"
    if [ "$reply" = "ok" ]; then
        echo "verified $alias"
    else
        echo "FAILED $alias -> $reply" >&2
        failures=$((failures + 1))
    fi
done

if [ "$failures" -gt 0 ]; then
    echo
    echo "$failures step(s) failed" >&2
    exit 1
fi
echo
echo "All models configured and verified on $EDGE_BASE"
