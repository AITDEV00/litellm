# Step 2: the user installs the client binary

The user runs `curl -fsSL https://litellm.ecouncil.ae/install/source-agent | sh`. Everything about this step is on the gateway and the user machine. The broker is not involved yet.

## 2a. The gateway serves a rendered installer

- Route: `GET /install/source-agent` and `GET /install/source-agent/{version}/{asset}`, in `litellm/proxy/client_distribution/source_agent_distribution.py`.
- Template: `oicm-litellm-layer/templates/source-agent-install.sh.tmpl`.
- The gateway reads the active release manifest from RustFS, validates its `release.json` metadata and recorded digest, and substitutes four placeholders. The gateway does not download and hash the installer binary while rendering this script; the installer checks the binary's SHA-256 locally after download:

| Placeholder | Filled with |
|---|---|
| `__BASE__` | the public base URL, so the installer fetches its assets from the same host it came from |
| `__VERSION__` | the active release version |
| `__SHA__` | the SHA-256 of the `adma` binary |
| `__STATE__` | the state directory, default `$HOME/.config/source-agent` |

If any placeholder survives rendering, the handler returns 500 rather than serve a script that would resolve nothing at run time.

## 2b. The script does the minimum

- Makes `~/.config/source-agent` and `~/.local/bin`, both owner-only where appropriate.
- Downloads `adma` and `release.json` to the state dir under a `.new` name, then moves them into place.
- Computes SHA-256 of the downloaded binary and compares to `__SHA__`; mismatch exits 1.
- `install -m 0755 adma` into `~/.local/bin`.
- Writes `~/.config/systemd/user/source-agent.service` with `ExecStart=<bin> connector`, `Restart=always`, `NoNewPrivileges=yes`.
- If stdin is a TTY, it runs `adma account add` and `adma source add` and enables the unit. If stdin is a pipe (the `curl | sh` case), it prints those commands instead, because both need a terminal.

The script contains **no configuration logic**. Every prompt, auth exchange, and identity write lives in the binary, so changing a question never needs a new script.

## 2c. `adma account add <alias>`

- Code: `internal/auth/auth.go`, `PasswordLogin.Password`.
- Request: `POST <gateway>/sso/cli/password` with `{"username": ..., "password": ...}`.
- Response: `{"key": "<scoped virtual key>"}`.
- Why not the dashboard login: the dashboard session is a short-lived browser JWT cookie; a daemon needs a durable credential.
- What is stored: the key only, at mode 0600, in `internal/state/state.go`'s account store. The password is never persisted.
- Error handling: on 401/403/429 it surfaces the status only, never the body, so a mistyped password cannot be echoed back through the terminal.
- `password_reset_required`, `proxy_admin`, and `proxy_admin_viewer` are rejected by this password-login path. It does not enforce `user_role == internal_user`, so an org-admin membership on an ordinary `internal_user` row is not itself blocked.
- Scope the connector key to `/mcp-sources` and `/mcp-source-link-sources/*` and keep its owner separate from unrelated provider credentials. Role alone is not the connector's effective least privilege.
- Treat this as a network-delivered installer: use only the reviewed gateway release URL and verify the release's checked-in signed provenance/checksum separately before installation changes. New security automation must not download and run a remote script in one step
## 2d. `adma source add --target <host:port> [--kind mcp --path /mcp | --kind openapi --spec file]`

- Code: `internal/setup/spec.go` (catalog fetch), `internal/bootstrap/client.go` (registration), `internal/state/state.go` (persistence).

Step 1, get the catalog if this is an OpenAPI source:

- Default: fetch `http://<target>/openapi.json` over plain HTTP from the agent's own machine, bounded at 8 MiB, non-2xx is a hard error.
- A `--spec <file>` wins over the fetch, so an operator can pin a reviewed document.
- An MCP source has no catalog and skips this.
- The Host header is set to the target's own authority so a service-routed or DNS-rebinding-protected origin still resolves correctly.

Step 2, enroll the source with the gateway, over public HTTPS, using the user's own key:

```
POST /mcp-sources
Authorization: Bearer <user key>
{"alias": "<derived>", "target": {"host": ..., "port": ...}, "kind": "openapi|mcp", "path": "...", "spec": "..."}
```

- Handler: `enroll` in `litellm/proxy/mcp_source_broker/gateway.py`.
- The gateway derives the owner from the bearer key's user id, not from the body.
- It derives a stable, collision-safe source name from the account and target.
- For OpenAPI it uploads the spec as an artifact to RustFS and references it by `spec_path`; for MCP there is no artifact.
- It writes an MCP server row: `url = http://litellm-mcp-broker...:8888/sources/<source_id>` for OpenAPI (or `.../<path>` for MCP), `credentials = {"auth_value": <service-token>}`, `mcp_access_groups = ["mcp-source-<source_id>"]`, `approval_status = pending_review`.
- Response: `201 {"source_id": ..., "owner": ..., "status": "pending_review"}`.
- Idempotent: a repeat by the same owner for the same normalized target returns the stored row and creates nothing new. Different content or target is 409.

Step 3, mint the connector identity:

```
POST /mcp-source-link-sources/<source_id>/workers
Authorization: Bearer <user key>
{"label": "<derived from name>", "dataset_id": ..., "target": {"host": ..., "port": ...}}
```

- Response: `{"worker_id", "source_id", "target", "generation", "credential"}`.
- The `credential` is 64 hex chars, derived from the pepper. Only its digest is stored in Postgres.
- The gateway authorizes proxy admin **or** the real owner of that source, so an ordinary user key can register its own connector. In `litellm/proxy/auth/route_checks.py` both `/mcp-sources` and the `/workers` suffix are marked "authN/authZ handled by api itself" for exactly this reason.
- `validateIssued` in the Go client rejects any response missing a field, naming a different source, carrying an invalid target, or whose credential is not 64 hex chars. The point is to fail now, not on the next restart after the bad identity was persisted.

Step 4, persist and announce:

- The identity is written atomically to `~/.config/source-agent/sources.json`, owner-only.
- `signalReload` sends SIGHUP to a running `adma connector` so it picks up the new source without a restart.

## 2e. What step 2 leaves behind

| Side | Result |
|---|---|
| user machine | `adma` on PATH, one account key, zero or more registered sources with worker identities, one systemd user unit |
| gateway DB | one `LiteLLM_MCPServerTable` row per source in `pending_review`, one `OicmMcpSource`, one `OicmMcpRemoteWorker`, one `OicmMcpEnrollment` anchor |
| RustFS | one catalog artifact per OpenAPI source |
| broker | still nothing. No connector has dialed yet |

Nothing is callable until an admin approves the source in step 6 of the usage guide.

## 2f. Security properties to notice

- The user key never crosses into the broker.
- The pepper never reaches the user machine.
- The password is never stored.
- Approval is separate from enrollment: self-service registration lands in `pending_review` regardless of who made the call.
- The alias grammar forbids `-` because the alias becomes the MCP server name which prefixes tool names, and `MCP_TOOL_PREFIX_SEPARATOR` forbids `-`; this was a real defect fixed with an alias pattern change.

## Files referenced in this step

- `litellm/proxy/client_distribution/source_agent_distribution.py`
- `oicm-litellm-layer/templates/source-agent-install.sh.tmpl`
- `oicm-litellm-layer/mcp-source-link/internal/auth/auth.go`
- `oicm-litellm-layer/mcp-source-link/internal/setup/spec.go`
- `oicm-litellm-layer/mcp-source-link/internal/bootstrap/client.go`
- `oicm-litellm-layer/mcp-source-link/internal/state/state.go`
- `oicm-litellm-layer/mcp-source-link/main.go` (`runAccountAdd`, `runSourceAdd`, `signalReload`)
- `litellm/proxy/mcp_source_broker/gateway.py` (`enroll`)
- `litellm/proxy/mcp_source_broker/remote_gateway.py` (workers route)
- `litellm/proxy/mcp_source_broker/models.py` (target grammar, alias grammar)
- `litellm/proxy/auth/route_checks.py`
- `oicm-litellm-layer/docs/usage-guides/MCP_SOURCE_AGENT_GUIDE.md`
