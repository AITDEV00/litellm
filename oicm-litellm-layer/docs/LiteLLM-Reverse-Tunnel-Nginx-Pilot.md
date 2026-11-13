# Test an Nginx data source through LiteLLM’s reverse tunnel

Server A runs a small Nginx instance and a Chisel client. A opens a TLS WebSocket through the gateway ingress. LiteLLM reads the Nginx response through a private relay port and generates the MCP tool from an OpenAPI file stored on the gateway side.

Run this on a staging gateway or pilot replica with an existing HTTPS ingress and an existing LiteLLM model alias. Use Bash. You’ll need Nginx, OpenSSL, and curl on A, Chisel on both sides, and curl and jq where you run the probes.

**Lab verification.** The Nginx source, both ingress snippets, the direct tool probe, and the inference request passed with Nginx `1.26.3`, Caddy `2.11.9`, Chisel `1.12.0`, and LiteLLM `1.108.6`. The gateway ran in a loopback-only network namespace, and the inference request used a mock tool-calling model. A returned a fresh value after its connector rejoined. Your firewall and deployed model are what this staging pilot checks. [LiteLLM release](https://github.com/BerriAI/litellm/releases/tag/v1.108.6)

**Check the ingress rule first.** A initiates the only cross-boundary connection. HTTP requests from the gateway still travel outward as data over that established WebSocket. The ingress and firewall policy must permit this flow on the worker route. An ingress restricted to `POST /v1/chat/completions` requires an approved worker route before this pilot can run. [WebSocket protocol](https://www.rfc-editor.org/rfc/rfc6455.html) · [Chisel reverse forwarding](https://github.com/jpillora/chisel/tree/v1.12.0)

| Address | Where it runs | Purpose |
|---|---|---|
| `127.0.0.1:18080` | A | Nginx serves `/source.json` |
| `https://gateway.example.com/mcp-worker` | Existing gateway ingress | A opens its tunnel here |
| `127.0.0.1:8088` | Gateway side | Ingress forwards the upgrade to Chisel |
| `127.0.0.1:17001` | Gateway side | Private listener forwards HTTP bytes to A’s Nginx |
| `127.0.0.1:4000` | Gateway side | Example address for the existing LiteLLM proxy |

The example uses a shared gateway network namespace for the relay and LiteLLM. Run them as host processes on one VM or put them in the same Kubernetes pilot pod so they share the private `17001` listener. Separate Docker containers have separate loopback addresses unless configured to share a network namespace. If the Kubernetes ingress controller runs separately, bind the relay’s `8088` ingress listener to the pod interface and route to it through a private Service restricted to the ingress controller. Keep `17001` loopback-only and leave it out of the Service. [Kubernetes pod networking](https://kubernetes.io/docs/concepts/workloads/pods/#pod-networking)

## 1. Start Nginx on Server A

If you need the packages on Debian or Ubuntu, run `sudo apt-get update` and `sudo apt-get install nginx curl openssl`. Run the following as a regular user to start a separate instance on the high port. Keep this shell open so you can inspect the log and stop the instance later. [Nginx command-line switches](https://nginx.org/en/docs/switches.html)

```bash
SOURCE_PILOT_DIR="$(mktemp -d)"
mkdir -p "$SOURCE_PILOT_DIR"/{html,logs}
SOURCE_VALUE="server-a-$(openssl rand -hex 8)"
printf '{"source":"server-a","value":"%s"}\n' "$SOURCE_VALUE" \
  > "$SOURCE_PILOT_DIR/html/source.json"

cat > "$SOURCE_PILOT_DIR/nginx.conf" <<'NGINX'
worker_processes 1;
pid logs/nginx.pid;
error_log logs/error.log notice;

events {
    worker_connections 64;
}

http {
    access_log logs/access.log combined;
    client_body_temp_path logs/client-body;
    proxy_temp_path logs/proxy;
    fastcgi_temp_path logs/fastcgi;
    uwsgi_temp_path logs/uwsgi;
    scgi_temp_path logs/scgi;

    server {
        listen 127.0.0.1:18080;

        location = /source.json {
            root html;
            default_type application/json;
            add_header Cache-Control no-store;
        }

        location / {
            return 404;
        }
    }
}
NGINX

nginx -p "$SOURCE_PILOT_DIR/" -c "$SOURCE_PILOT_DIR/nginx.conf" -e stderr -t &&
nginx -p "$SOURCE_PILOT_DIR/" -c "$SOURCE_PILOT_DIR/nginx.conf" -e stderr &&
curl --noproxy 127.0.0.1 -fsS --max-time 5 \
  http://127.0.0.1:18080/source.json
```

Record the value returned on A. The example below represents a generated value. Each run produces a different one.

```json
{"source":"server-a","value":"server-a-2d6390a4b5d7e8f1"}
```

## 2. Start the relay on the gateway side

Stage Chisel `v1.12.0` on A and beside LiteLLM. On a connected Linux x86-64 machine, this downloads and verifies the exact release binary. Use the matching release asset and checksum for other architectures. Import the verified binary through your normal artifact process. [Chisel v1.12.0 release](https://github.com/jpillora/chisel/releases/tag/v1.12.0)

```bash
(
  set -eu
  curl -fSL --max-time 90 -o chisel.gz \
    https://github.com/jpillora/chisel/releases/download/v1.12.0/chisel_1.12.0_linux_amd64.gz
  printf '%s  %s\n' \
    f3f180f1d93aa72cce4e6386f98cc06569a0146fbd65eb4423cf83e6434bcfe6 chisel.gz \
    | sha256sum -c -
  gzip -dc chisel.gz > chisel
  chmod 0755 chisel
  ./chisel --version
)
```

Run this next to LiteLLM, from the directory containing the staged binary. Keep the relay running in this terminal. A’s credential is scoped to the private `17001` listener. Record the relay fingerprint shown in the logs and provide A with its credential through your approved secret distribution process. [Chisel relay authentication and reverse ports](https://github.com/jpillora/chisel/tree/v1.12.0)

```bash
RELAY_PILOT_DIR="$(mktemp -d)"
(
  set -eu
  umask 077
  SOURCE_A_CREDENTIAL="source-a:$(openssl rand -hex 24)"
  printf '%s\n' "$SOURCE_A_CREDENTIAL" > "$RELAY_PILOT_DIR/source-a.auth"
  printf '{"%s":["^R:127[.]0[.]0[.]1:17001$"]}\n' "$SOURCE_A_CREDENTIAL" \
    > "$RELAY_PILOT_DIR/relay-users.json"
  ./chisel server --keygen "$RELAY_PILOT_DIR/relay.key"
)
printf 'Connector credential file is %s\n' "$RELAY_PILOT_DIR/source-a.auth"

./chisel server --host 127.0.0.1 --port 8088 --reverse \
  --authfile "$RELAY_PILOT_DIR/relay-users.json" \
  --keyfile "$RELAY_PILOT_DIR/relay.key"
```

## 3. Add the worker path to the HTTPS ingress

Add one of these snippets to the existing TLS virtual host. Use the same HTTPS hostname and port as Chat Completions. Use the relay address visible to your ingress if it runs in another network namespace.

**Nginx ingress** · Add this location inside the existing `server` block. Validate the config and reload through your normal deployment process. [Nginx WebSocket proxying](https://nginx.org/en/docs/http/websocket.html)

```nginx
location = /mcp-worker {
    proxy_pass http://127.0.0.1:8088;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 120s;
}
```

**Caddy ingress** · Add this handler inside the existing HTTPS site block. [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)

```caddyfile
handle /mcp-worker {
    reverse_proxy 127.0.0.1:8088
}
```

## 4. Connect A and prove the tunnel with curl

In a second terminal on A, run this from the directory containing `chisel`. Set `GATEWAY_URL` to your HTTPS ingress without a trailing slash. For an internal CA, add `--tls-ca /path/to/ingress-ca.pem` before the URL argument. Chisel verifies the ingress certificate and pins the relay fingerprint. Leave the client running after it logs `Connected`. [Chisel client options](https://github.com/jpillora/chisel/tree/v1.12.0)

```bash
GATEWAY_URL='https://gateway.example.com'
read -r -p 'Relay fingerprint: ' RELAY_FINGERPRINT
read -r -s -p 'Server A connector credential: ' RELAY_CREDENTIAL
printf '\n'

AUTH="$RELAY_CREDENTIAL" ./chisel client \
  --fingerprint "$RELAY_FINGERPRINT" \
  --keepalive 25s \
  "$GATEWAY_URL/mcp-worker" \
  'R:127.0.0.1:17001:127.0.0.1:18080'

unset RELAY_CREDENTIAL
```

On the gateway side, run this in the relay’s network namespace. If your LiteLLM image lacks curl, run it in the relay container or an approved debug container in the same pod.

```bash
probe_source() {
  curl --noproxy 127.0.0.1 -fsS --connect-timeout 2 --max-time 5 \
    http://127.0.0.1:17001/source.json
}

probe_source | jq -e 'select(.source == "server-a" and (.value | test("^server-a-[0-9a-f]{16}$")))'
```

**Pass.** The JSON contains the exact value printed on A. In the Nginx shell on A, `tail -n 5 "$SOURCE_PILOT_DIR/logs/access.log"` shows a fresh `GET /source.json` with HTTP 200. The network test is complete at this point. You can do the disconnect and reconnect checks in step 7 before adding the MCP tool.

## 5. Register the tool inside LiteLLM

Save this JSON as `nginx-source.openapi.json` on the gateway side and mount it where the LiteLLM process can read it. The only operation is a fixed GET for the marker. [LiteLLM MCP from OpenAPI specs](https://docs.litellm.ai/docs/mcp_openapi/)

```json
{
  "openapi": "3.0.3",
  "info": {"title": "Server A Nginx pilot", "version": "1.0.0"},
  "paths": {
    "/source.json": {
      "get": {
        "operationId": "read_source",
        "summary": "Read the live pilot marker from Server A.",
        "responses": {
          "200": {
            "description": "The current Server A marker",
            "content": {
              "application/json": {
                "schema": {
                  "type": "object",
                  "required": ["source", "value"],
                  "properties": {
                    "source": {"type": "string"},
                    "value": {"type": "string"}
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

Merge this entry into the existing `mcp_servers` section of the pilot LiteLLM config and adjust `spec_path` to the absolute path inside that process. Reload or redeploy the pilot using your normal procedure. Grant the pilot key access to `nginx_source_a` and to the model alias you’ll use. [LiteLLM MCP configuration](https://docs.litellm.ai/docs/mcp_config_reference) · [LiteLLM MCP permissions](https://docs.litellm.ai/docs/mcp_control)

```yaml
mcp_servers:
  nginx_source_a:
    transport: http
    url: http://127.0.0.1:17001
    spec_path: /etc/litellm/pilot/nginx-source.openapi.json
    allowed_tools: [read_source]
```

Run the direct gateway probe from the pilot’s network namespace using an authorized key. This checks that LiteLLM loaded the definition and can execute the generated tool without a model. Keep these probe routes private. [LiteLLM REST tool routes at v1.108.6](https://github.com/BerriAI/litellm/blob/v1.108.6/litellm/proxy/_experimental/mcp_server/rest_endpoints.py)

```bash
LITELLM_LOCAL='http://127.0.0.1:4000'
read -r -s -p 'Authorized LiteLLM pilot key: ' LITELLM_PILOT_KEY
printf '\n'

curl --noproxy 127.0.0.1 -fsS --max-time 10 \
  -H "Authorization: Bearer $LITELLM_PILOT_KEY" \
  "$LITELLM_LOCAL/mcp-rest/tools/list?mcp_server_name=nginx_source_a" \
  | jq -e '.tools[] | select(.name == "read_source") | .name'

curl --noproxy 127.0.0.1 -fsS --max-time 10 \
  -H "Authorization: Bearer $LITELLM_PILOT_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary '{"server_id":"nginx_source_a","name":"read_source","arguments":{}}' \
  "$LITELLM_LOCAL/mcp-rest/tools/call" | jq .
```

The list returns `read_source`. The call returns the current marker in the tool result. Check the value against A and look for the corresponding GET in A’s Nginx log.

## 6. Call the tool through the existing inference route

Run this from a client that can reach your existing ingress. Use the alias of a model that supports tool calling. The prompt leaves out the generated value so you can distinguish a live tool result from a guess. If your ingress uses a private CA, add the corresponding `--cacert` argument to curl. [LiteLLM MCP with Chat Completions](https://docs.litellm.ai/docs/mcp/)

```bash
GATEWAY_URL='https://gateway.example.com'
read -r -p 'Tool-capable LiteLLM model alias: ' LITELLM_PILOT_MODEL
read -r -s -p 'Authorized LiteLLM pilot key: ' LITELLM_PILOT_KEY
printf '\n'

jq -n --arg model "$LITELLM_PILOT_MODEL" '{
  model: $model,
  stream: false,
  messages: [{
    role: "user",
    content: "Call the read_source tool now. Return only its value field exactly as received."
  }],
  tools: [{
    type: "mcp",
    server_label: "nginx_source_a",
    server_url: "litellm_proxy/mcp/nginx_source_a",
    require_approval: "never"
  }]
}' | curl --fail-with-body --silent --show-error --max-time 40 \
  -H "Authorization: Bearer $LITELLM_PILOT_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary @- "$GATEWAY_URL/v1/chat/completions" \
  | jq -er '.choices[0].message.content'
```

**Pass.** The response contains A’s current value and A’s access log shows a fresh GET. If the direct gateway probe passes and this step fails, inspect model tool support, model alias, and the pilot key’s model and MCP grants.

## 7. Disconnect, reconnect, and inspect the firewall logs

1. Stop only the Chisel client on A with `Ctrl+C`. Keep Nginx and LiteLLM running. After the relay logs the disconnect, `probe_source` on the gateway should fail. The local Nginx curl from step 1 should still succeed.
2. On A, generate a new value and replace the static file atomically.

   ```bash
   SOURCE_VALUE="server-a-$(openssl rand -hex 8)"
   printf '{"source":"server-a","value":"%s"}\n' "$SOURCE_VALUE" \
     > "$SOURCE_PILOT_DIR/html/.source.json.next"
   mv "$SOURCE_PILOT_DIR/html/.source.json.next" "$SOURCE_PILOT_DIR/html/source.json"
   printf '%s\n' "$SOURCE_VALUE"
   ```

3. Start the same Chisel client command again. Repeat `probe_source` and the inference call. Both should return the new value through the same private listener without a LiteLLM restart.
4. In the firewall or ingress logs, confirm that A initiated the session to the ingress HTTPS port and the worker route upgraded to WebSocket with `101 Switching Protocols`. Check that there were no new gateway-initiated sessions to A. The loopback-only Nginx listener keeps port `18080` private on A. [WebSocket upgrade status](https://nginx.org/en/docs/http/websocket.html)

| Symptom | Where to check |
|---|---|
| A’s local curl fails | Nginx config test, `logs/error.log`, and whether `18080` is in use |
| Chisel reports `bad handshake` | Worker route match, proxy upgrade, and ingress policy |
| Chisel reports a certificate or fingerprint error | Ingress CA trust, hostname, and the relay fingerprint |
| Chisel connects but the raw curl fails | Reverse mapping, relay logs, and whether the probe runs in the relay’s network namespace |
| Raw curl passes and the direct tool call fails | OpenAPI mount, `spec_path`, LiteLLM logs, and the pilot key’s MCP grant |

To clean up, stop A’s connector and the relay, then remove the pilot ingress route and MCP entry. Stop the pilot Nginx instance from its original shell with `nginx -p "$SOURCE_PILOT_DIR/" -c "$SOURCE_PILOT_DIR/nginx.conf" -e stderr -s quit`.
