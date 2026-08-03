BROKER_NS=submariner-k8s-broker
URL=$(kubectl -n default get endpoints kubernetes \
  -o jsonpath="{.subsets[0].addresses[0].ip}:{.subsets[0].ports[?(@.name=='https')].port}")
# CA: already base64 in the secret -> wrap AGAIN so it's paste-safe (one clean line)
CA_WRAPPED=$(kubectl -n "$BROKER_NS" get secret submariner-k8s-broker-client-token \
  -o jsonpath='{.data.ca\.crt}' | tr -d '[:space:]' | base64 -w0)
# TOKEN: decode the JWT, then wrap it so paste can't touch the dots
TOK_WRAPPED=$(kubectl -n "$BROKER_NS" get secret submariner-k8s-broker-client-token \
  -o jsonpath='{.data.token}' | base64 -d | tr -d '[:space:]' | base64 -w0)

echo "===BROKER-BLOB-START==="
echo "URL=${URL}"
echo "CAW=${CA_WRAPPED}"
echo "TOKW=${TOK_WRAPPED}"
echo "===BROKER-BLOB-END==="