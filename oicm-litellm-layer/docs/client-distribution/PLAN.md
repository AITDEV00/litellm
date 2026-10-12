# Same-Origin Client Distribution — Design Plan

Status: design, not implemented. This folder is the plan of record for serving the
LiteLLM CLI (`lite`) and its install chain from the OICM gateway, so a
locked-down developer machine whose only reachable host is `litellm.ecouncil.ae`
can go from bare OS to a configured coding agent with one command and zero
external network access.

Everything here is designed against the current source tree
(`jya0-v1.102.0`) and reuses existing upstream components wherever they exist.

## Revision note (v2)

The first draft put the artifacts on a separate static-serving pod routed by
nginx path. Review rejected that as overengineering: the gateway already
serves its own Admin UI via `StaticFiles` and OICM custom routes via an
`APIRouter` slice, so the same-origin chain now lives **inside the running
litellm gateway as a vertical slice** (`litellm/proxy/client_distribution/`),
pinned by the standard drop-detection tests. One deployment, one ingress
rule, and `/install` becomes a generated artifact that can read the
gateway's own state (config, DB, request host). See
[architecture.md](architecture.md) for the decision record.

## Revision note (v3)

Three decisions from review, folded in:

1. **Storage is a PVC, not MinIO/S3.** LiteLLM has S3 support but only as a
   *logging* callback (`integrations/s3.py`, `s3_v2.py` write spend/audit logs
   to a bucket via `put_object`; there is no serve/download path). Using MinIO
   purely to hold a few CLI artifacts, when nothing else uses it beyond logs,
   is overkill. A PVC keeps it simple and matches the repo's existing
   PVC + CronJob patterns. See [mirror-content.md](mirror-content.md) and
   [deployment.md](deployment.md).
2. **Admin-facing sync API on the gateway.** Because a PVC can be deleted
   accidentally, the slice exposes admin-authenticated CRUD routes so an admin
   with internet access can push, upsert, or delete artifacts over REST,
   idempotently, using the gateway's master key as the bearer token. This
   replaces "admin copies bytes with `kubectl cp`" with a documented API. See
   [admin-sync.md](admin-sync.md).
3. **Harness detection is a reporter, not a gate.** The script detects
   installed harnesses (cross-platform) and reports them; it does not
   auto-select one. Auto-selection was considered and dropped as a product
   choice, left to the user. See [installer-script.md](installer-script.md).

## Problem

Developer machines inside the firewall can reach `litellm.ecouncil.ae` (443) and
nothing else. The upstream `lite` install flow assumes unrestricted egress:

```
raw.githubusercontent.com -> install-cli.sh
astral.sh                 -> uv installer
github.com                -> uv release tarballs (via the installer)
PyPI                      -> litellm[cli] + dependencies
github.com                -> managed CPython (python-build-standalone, via uv)
```

Every hop except the first script itself is a blocked host, so the official
installer fails on these machines after the first `curl`.

## Goal and non-goals

Goal: `curl -fsSL https://litellm.ecouncil.ae/install | sh` installs a working
`lite`, and every subsequent network operation the CLI performs stays on
`litellm.ecouncil.ae`. The end user never needs to know that `uv`, a wheelhouse,
or a Python mirror exist.

Non-goals:

- No fork of upstream `litellm[cli]`. All client behavior stays the stock CLI
  (`lite configure`, `lite login`, `lite claude/codex/opencode/pi`, model sync
  via `/v1/models`). We distribute; we do not modify. The only new code is the
  gateway-side serving slice.
- No per-user personalized install scripts in v1. The served script is
  generic (with the gateway URL and adopted versions generated from gateway
  state); the per-key personalization already happens client-side at runtime
  (`agent_model_sync_env` fetches `/v1/models` with the user's key). The
  slice's in-app position makes authenticated/personalized variants a
  possible future extension — recorded, not built.
- No new auth surface in v1. `/install` and the artifact routes are
  unauthenticated (see [security.md](security.md) for the reasoning and the
  bootstrap paradox).
- No guarantee of literal universality. See [platform-matrix.md](platform-matrix.md)
  for the supported matrix and the Android/iOS stance.

## Design in one paragraph

Keep upstream's exact installation mechanism and move its origins. `uv` is the
only piece of the chain that is a genuine standalone binary, so we mirror the
pinned `uv` release artifacts; we mirror the `litellm[cli]` wheel closure as a
flat wheelhouse; and we mirror the exact CPython builds `uv` would fetch. All
of it is served by a vertical slice inside the running gateway: `GET /mirror/*`
is a `StaticFiles` mount on a read-only artifact volume, and `GET /install` is
a router handler that generates the bootstrap script at request time, baking
in the gateway's own base URL and the currently adopted versions from
`manifest.json`. The script body is upstream's `scripts/install-cli.sh` with
three origin changes (`INSTALLER_DOWNLOAD_URL` for the uv bootstrap,
`--no-index --find-links` for the wheelhouse, `UV_PYTHON_INSTALL_MIRROR` for
managed Python), then `lite config set base_url` pre-configures the CLI and
the stock flow takes over. No modification of litellm's or uv's logic; one new
slice, two wiring lines, drop-detection tests.

## Document map

| Doc | Purpose |
|-----|---------|
| [architecture.md](architecture.md) | Host model, the in-gateway slice, why in-app won, the full flow |
| [mirror-content.md](mirror-content.md) | Exact bytes we must serve: uv artifacts, wheelhouse, Python builds, checksums, manifest |
| [installer-script.md](installer-script.md) | Line-by-line design of the generated `/install` script, derived from upstream `scripts/install-cli.sh` |
| [platform-matrix.md](platform-matrix.md) | Supported OS/arch matrix and the Android/Termux/iOS stance |
| [deployment.md](deployment.md) | Artifact volume, gateway pod mount, admin sync API, rollout |
| [admin-sync.md](admin-sync.md) | Admin REST API to push/upsert/delete mirror artifacts idempotently |
| [security.md](security.md) | Threat model, checksums, signature question, why static unauth is OK |
| [rollout.md](rollout.md) | Phased rollout: pilot users, success metrics, cut-over |
| [open-questions.md](open-questions.md) | Unresolved decisions that need a human call |
