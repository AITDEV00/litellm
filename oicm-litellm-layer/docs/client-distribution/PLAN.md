# Same-Origin Client Distribution — Design Plan

Status: design, not implemented. This folder is the plan of record for serving the
LiteLLM CLI (`lite`) and its install chain from the OICM gateway hostname, so a
locked-down developer machine whose only reachable host is `litellm.ecouncil.ae`
can go from bare OS to a configured coding agent with one command and zero
external network access.

Everything here is designed against the current source tree
(`jya0-v1.102.0`) and reuses existing upstream components wherever they exist.

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
  via `/v1/models`). We distribute; we do not modify.
- No per-user personalized install scripts. The served script is generic; the
  per-key personalization already happens client-side at runtime
  (`agent_model_sync_env` fetches `/v1/models` with the user's key). That
  boundary was settled in the earlier research: a plain `curl` of a static
  path carries no caller identity, so per-key adaptation must live where the
  key lives.
- No new auth surface. `/install` and the artifact routes below are
  unauthenticated static files (see [security.md](security.md) for the
  reasoning and the key-verification step that happens after install, inside the
  CLI).
- No guarantee of literal universality. See [platform-matrix.md](platform-matrix.md)
  for the supported matrix and the Android/iOS stance.

## Design in one paragraph

Keep upstream's exact installation mechanism and move its origins. `uv` is the
only piece of the chain that is a genuine standalone binary, so we mirror the
pinned `uv` release artifacts behind the gateway; we mirror the `litellm[cli]`
wheel closure as a flat wheelhouse; and we mirror the exact CPython builds `uv`
would fetch. The served `/install` script is a lightly-modified copy of
upstream's `scripts/install-cli.sh`, changed only to (1) point the uv bootstrap
at the gateway mirror via `INSTALLER_DOWNLOAD_URL`, (2) run
`uv tool install --find-links <gateway wheelhouse> --no-index`, and (3) pin
Python via the gateway's managed-Python mirror. After install, `lite` is
pointed at the gateway with `lite config set base_url https://litellm.ecouncil.ae`
and the stock CLI takes over. No code changes to litellm or uv; only mirror
content and an installer script.

## Document map

| Doc | Purpose |
|-----|---------|
| [architecture.md](architecture.md) | The full flow: ingress routing, mirror layout, install script anatomy, what runs where |
| [mirror-content.md](mirror-content.md) | Exact bytes we must serve: uv artifacts, wheelhouse, Python builds, checksums, manifest |
| [installer-script.md](installer-script.md) | Line-by-line design of the served `/install` script, derived from upstream `scripts/install-cli.sh` |
| [platform-matrix.md](platform-matrix.md) | Supported OS/arch matrix and the Android/Termux/iOS stance |
| [deployment.md](deployment.md) | Ingress paths, the artifact pod, mirror sync job, rollout |
| [security.md](security.md) | Threat model, checksums, signature question, why static unauth is OK |
| [rollout.md](rollout.md) | Phased rollout: pilot users, success metrics, cut-over |
| [open-questions.md](open-questions.md) | Unresolved decisions that need a human call |
