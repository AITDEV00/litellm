#!/bin/sh
set -eu

umask 077
host_key=$(printf %s "${MCP_SOURCE_BROKER_HOST_KEY:?relay host key is required}" | tr -d '\r\n')
authfile=${MCP_SOURCE_BROKER_AUTHFILE:-/tmp/mcp-source-broker/chisel-users.json}
binary=${CHISEL_BIN:-chisel}
python_bin=${PYTHON_BIN:-python}

mkdir -p "$(dirname "$authfile")"
chmod 700 "$(dirname "$authfile")"
touch "$authfile"
chmod 600 "$authfile"
cd "$(dirname "$authfile")"
: "${MCP_SOURCE_BROKER_SERVICE_TOKEN:?broker service token is required}"
export AUTH="relay-service:${MCP_SOURCE_BROKER_SERVICE_TOKEN}"
"$python_bin" -m litellm.proxy.mcp_source_broker.relay_auth --once
"$python_bin" -m litellm.proxy.mcp_source_broker.relay_auth &
exec "$binary" server --host 0.0.0.0 --port 8088 --reverse --authfile "$authfile" --keyfile "$host_key" --keepalive 30s --pid
