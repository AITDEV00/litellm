# Same-Origin Client Distribution — Design Plan

Status: design, single plan of record. This folder describes serving
**cc-switch-cli** and its install chain from the OICM gateway, so a
locked-down developer machine whose only reachable host is
`litellm.ecouncil.ae` can go from bare OS to a configured coding agent with
one command and zero external network access.

The previous design (uv + Python + `litellm[cli]` + the `lite` CLI) is
superseded and archived in [history/lite-cli-PLAN.md](history/lite-cli-PLAN.md).
It is kept for context only; nothing below depends on it.

Everything here is designed against the current source tree
(`jya0-v1.102.0`) and reuses existing upstream components wherever they exist.

## The one-paragraph design

`/install` serves a generated bash script. It downloads the cc-switch static
binary from the gateway mirror, prompts for the user's gateway virtual key and
validates it against `/v1/models`, asks which harness(es) to configure, then
uses cc-switch to register our gateway as a provider and write each chosen
harness's native config file. No uv, no Python, no wheelhouse, no `lite` CLI,
no login step — the user already holds their key.

## Why cc-switch over the `lite` CLI

- **Harness coverage.** cc-switch writes native config for 7 harnesses
  (claude, codex, gemini, open-code, hermes, open-claw, pi). `lite` only ever
  configured claude and codex; opencode/pi got an ephemeral env var, never a
  real `opencode.json`.
- **One static binary.** No Python runtime, no uv bootstrap, no wheelhouse, no
  managed-CPython mirror. The mirror shrinks from ~1 GB to ~35 MB.
- **It already solved the hard part.** cc-switch is a maintained
  provider-config engine with per-harness adapters, backup/restore, and
  idempotent-ish reconcile. We adopt it instead of hand-building the same in
  `lite`.

## The decisions that shape it

1. **Everything under `/install`.** One prefix for the user and the firewall.
   Public reads (the script, the manifest, the cc-switch archives) and the
   admin write surface (`/install/admin/*`) all nest under it.
2. **The bootstrap template is baked into the gateway image.** RustFS holds
   versioned binaries + the manifest, never the shell developers pipe into
   `sh`. Store compromise then cannot become arbitrary code execution on
   developer machines (see [security.md](security.md)).
3. **The manifest is the client's integrity source of truth.** Admin sync
   verifies upstream's `checksums.txt` at ingest and records the verified hash
   in the manifest; the client checks downloads against the manifest only.
4. **cc-switch releases are immutable + versioned.** A
   `cc-switch/<version>/<file>` object, once published with a given sha256, is
   never overwritten with different bytes. The manifest publishes with a
   generation/ETag so concurrent admin syncs cannot clobber each other.
5. **One command is both install and resync.** Re-running `/install`
   re-validates the key and re-syncs the provider's model list. Harness model
   lists are not live-read; reconciliation is explicit.
6. **Harness configure is per-harness, not one loop.** claude/codex/gemini are
   switch-mode (field flags); open-code/pi/hermes/open-claw are additive (raw
   `--config`). See [installer-script.md](installer-script.md).
7. **First release certifies claude, codex, open-code.** gemini/hermes/
   open-claw/pi are added after their config semantics are E2E-tested against
   our gateway; we do not advertise untested harnesses.
8. **`/install` is the authoritative update channel.** cc-switch's own
   `update` command points at public GitHub and is not used here.
9. **No upstream contribution.** Proprietary, `jya0-` branches only. The
   drop-detection tests matter because we maintain the fork ourselves.
10. **No harness mirroring.** cc-switch writes a harness's config; it never
    installs the harness. Users install Claude Code / OpenCode through their
    own channels.

## Problem

Developer machines inside the firewall can reach `litellm.ecouncil.ae` (443)
and nothing else. The upstream cc-switch install flow assumes unrestricted
egress (GitHub releases). We mirror the binaries and serve the whole chain
same-origin.

## Reading order

- [architecture.md](architecture.md) — the slice, the route map, the flow
- [installer-script.md](installer-script.md) — the four steps + the generator
  contract
- [mirror-content.md](mirror-content.md) — bucket layout + manifest schema 5
- [security.md](security.md) — threat model, trust split, credential state
- [admin-sync.md](admin-sync.md) — the operator's sync loop
- [platform-matrix.md](platform-matrix.md) — the four supported targets
- [deployment.md](deployment.md) — env vars + rollout
- [rollout.md](rollout.md) — pilot + GA
- [open-questions.md](open-questions.md) — deferred extensions
