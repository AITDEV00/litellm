# Dev source broker

This dev slice exposes one approved OpenAPI service on a source host through the existing MCP gateway. The source host makes an outbound Chisel TLS WebSocket to the broker relay. LiteLLM authenticates and authorizes the normal MCP tool request before the private broker dispatches it to a healthy member

The dev overlay is `deploy/overlays/dev/mcp-source-broker`. It creates one auxiliary Pod with broker and relay containers. The gateway contains no relay and starts no connector. Production is not enabled by this slice

## Enrollment

A proxy administrator posts a `SourceCreate` to `POST /mcp-sources`:


```json
{
  "owner": "source-owner",
  "alias": "private-openapi",
  "key": "admin-generated-at-least-16-chars",
  "expires_at": "2027-01-01T00:00:00Z",
  "target": {"host": "127.0.0.1", "port": 18080},
  "spec": "{\"openapi\":\"3.1.0\",\"paths\":{\"/source.json\":{\"get\":{}}}}"
}
```

The source target allows loopback only and requires an explicit local service. An ordinary source membership stays in the authenticated relay authfile even when unhealthy; quarantine prevents tool dispatch, not subsequent recovery. Administrators can retrieve the existing connector installer whenever the source has been catalog-quarantined

The source target is only a loopback host and port. Source identity is derived from `owner` and `alias`. The gateway saves the catalog object in the gateway-accessible artifact mirror, registers a stable private pool URL with LiteLLM, and grants that source its own MCP access group. Re-enrollment keeps its source, enrollment, connector lease, and relay credentials; it updates the target and expected canonical catalog fingerprint. It does not return another one-time token

The returned `install_path` is relative to the gateway. Fetch it with the one-time enrollment token only once:


```sh
curl -f "http://127.0.0.1:14002/mcp-source-broker/install/$ENROLLMENT_ID?token=$TOKEN" -o install-connector.sh
```

An authenticated owner or proxy administrator can retrieve the same persistent connector configuration without a token. The token is single-use and only its HMAC is saved. Treat the whole rendered installer as sensitive because it provisions relay credentials into a mode-600 environment file

Run the script as the ordinary user who owns the source service. It installs a Chisel binary downloaded through the HTTPS gateway, verifies its pinned SHA-256, writes `~/.config/systemd/user/mcp-source-connector.service`, and enables the user unit. It does not require `sudo`. Prerequisites are `curl`, `gzip`, `sha256sum`, user systemd, and the host's normal CA trust store

The broker control service is on `8089`, dispatch on `9090`, and Chisel relay on `8088`. Only the WebSocket relay and gateway ingress are public. Control and source dispatch require the independent broker service credential

## Calls


Use an ordinary virtual key authorized for the source-specific MCP access group. No Chisel installation is needed on the caller's machine. The gateway creates OpenAPI tools from the saved spec and dispatches them to the pool URL without a trailing slash


A healthy connector has a recent successful catalog probe and actual catalog fingerprint equal to the enrollment's expected fingerprint. Startup clears health timestamps. Different catalogs are quarantined rather than merged. The pool strips gateway authorization, cookies, and transport headers before forwarding. Retries are restricted to errors where the upstream connection was not established


The fixture proof can use this request with `READ_KEY`:


```sh
curl -fsS http://127.0.0.1:14003/mcp-rest/tools/call \
  -H "Authorization: Bearer $READ_KEY" \
  -H 'Content-Type: application/json' \
  --data '{"server_id":"f804e8c9-a9c7-5824-a90a-837262788be8","name":"read_source","arguments":{}}'
```

The returned `CallToolResult.content[0].text` contains JSON from the source's `/source.json` endpoint. For an echo tool, use `{"channel":"dev-tool","body":{"text":"round-trip-through-chisel"}}` as `arguments`. Validate the actual source marker and that its `credentials_seen` and `cookies_seen` booleans are false


## Verification


```sh
.venv/bin/python -m pytest -q tests/unit/proxy/mcp_source_broker
.venv/bin/python -m pytest -q oicm-litellm-layer/tests/deploy/test_dev_prod_parity.py
.venv/bin/python -m basedpyright litellm/proxy/mcp_source_broker
make -C oicm-litellm-layer litellm-src-release-dev
```

Build the auxiliary image from the exact running gateway digest after the gateway rollout completes. `LITELLM_SRC_BASE` explicitly selects that digest, so the same old base is not accidentally reused when the dev tag moves. The artifact sync checks the pinned checksum and gzip integrity. Its ignored `chisel.gz` belongs in the overlay directory, which is the complete Buildah context below. Keep `KUBECONFIG` pointing at the Al Ain kubeconfig for these commands


```sh
sh oicm-litellm-layer/scripts/sync-mcp-source-artifacts.sh
BASE=$(kubectl -n adeo-litellm get pods -l app=litellm-proxy-dev \
  -o jsonpath='{.items[?(@.status.phase=="Running")].status.containerStatuses[0].imageID}')
IMAGE=registry.adeoaiengine.ecouncil.ae/openinnovationai/platform/mlops/mlops-serving/litellm-mcp-broker:jya0-v1.108.5
podman build --pull=always --build-arg "LITELLM_SRC_BASE=$BASE" --tag "$IMAGE" \
  -f oicm-litellm-layer/deploy/overlays/dev/mcp-source-broker/Dockerfile \
  oicm-litellm-layer/deploy/overlays/dev/mcp-source-broker
podman push --tls-verify=false "$IMAGE"
kubectl -n adeo-litellm rollout restart deployment/litellm-mcp-broker-dev
```

Do not commit the downloaded `chisel.gz`. Its build input is checked by SHA-256 and gzip integrity. The existing gateway release workflow publishes only the dev overlay, never production

On 2026-11-13, the installed dev gateway and auxiliary image carried `read_source` and `echo_source` for source `f804e8c9-a9c7-5824-a90a-837262788be8`. The one-time enrollment claim minted connector `7f3a49c4-59a7-5611-8c4e-fff9b8a0e670` at `127.0.0.20:18081`. An ordinary same-identity re-enrollment refreshed the saved canonical fingerprint after catalog drift. It returned no new token or connector

An administrator retrieved the authenticated recovery script through both the public dev ingress and a local gateway forward. The identical script installed the connector as user `jyao` on `aitdev00`, verified Chisel `1.12.0`, wrote a mode-600 environment file, and generated a matching user systemd unit. No `sudo`, downloaded shell execution, disabled TLS verification, or temporary service command override was needed

A virtual key restricted to this source's MCP access group listed exactly `read_source` and `echo_source`. `read_source` returned hostname `aitdev00` and the persisted source marker. `echo_source` returned `round-trip-through-chisel` on channel `dev-tool`, with `credentials_seen=false` and `cookies_seen=false`. The same key received HTTP 403 for the other source and a non-admin key could not retrieve an installer. Direct dispatch without the service credential returned HTTP 401. An already-redeemed token was rejected, including the complete claim-rejection test in the unit suite

Local connector, dev auxiliary, and dev gateway restarts each recovered with the same source ID, connector ID, listener, and correct MCP responses. Control port 8089 and dispatch port 9090 were unreachable on the public host. Production workloads were not modified

The real-PostgreSQL store suite passed nine tests, including catalog-mismatch quarantine, owner recovery, and valid-fingerprint reactivation. Its temporary `postgres-regression` tenant rows were removed. For that opt-in developer test, forward only `svc/adeo-litellm-postgres-dev-rw` to local port 15432, build its `LITELLM_TEST_POSTGRES` URL from the existing dev credential in a protected temporary file, and unset the variable afterward. Run the store tests with `--confcutdir=tests/unit/proxy/mcp_source_broker` so ambient environment stripping does not select unrelated host fixtures. The normal test command keeps this database test skipped

Focused broker tests, deploy parity tests, strict Ruff, and basedpyright passed. The complete repository gates were attempted but did not pass: dependency resolution was blocked by the GitHub codeload DNS sinkhole, format checks reported inherited files, and default-branch discipline comparisons reported inherited findings. The strict Ruff worktree gate also lacked its expected configuration. These are not claimed as successful repository-wide checks

Multi-source host failover, native MCP catalog membership, authorization across source replicas, root/system connector installation, and multi-replica session ownership are not implemented or claimed by this dev slice. The README next to this document remains the earlier design proposal, not a release claim
