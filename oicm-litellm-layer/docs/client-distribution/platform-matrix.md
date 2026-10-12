# Supported Platform Matrix

`uv` is universal across normal developer operating systems, not literally
every OS. We guarantee the matrix below and label everything else honestly.

## Guaranteed (tier 1 support)

| OS / arch | uv upstream status | Notes |
|---|---|---|
| Linux x86-64 (glibc) | Tier 1 | Primary target (the org's standard dev machines) |
| Linux ARM64 (glibc) | Tier 2 + official binary | |
| Linux x86-64 musl (Alpine) | Official static binary | Static musl build avoids host libc entirely |
| macOS Apple Silicon | Tier 1 | |
| macOS Intel | Tier 1 | |

## Supported on request (tier 2)

| OS / arch | uv upstream status | Notes |
|---|---|---|
| Windows x64 | Tier 1 | Needs `/install.ps1`; no Windows user behind the firewall yet |
| Windows ARM64 | Tier 2 | Same |
| Linux ARMv7 | Tier 2 + official binary | Only if real hardware shows up |

## Explicitly not promised

| Environment | Why |
|---|---|
| Android / Termux | `pkg install uv` exists in Termux, but Astral ships no `aarch64-linux-android` release artifact and Android host support has had real breakage (e.g. uv initially rejecting `sys.platform == "android"` after Termux's Python 3.13 move). Possible, not part of the enterprise matrix. |
| iOS / iPadOS | Not a supported native uv host, and not a sensible local CLI host anyway. An iPad user SSHes into a Linux box, where this design works. uv's `--python-platform` iOS targets are about cross-building from a dev machine, not running on-device. |
| FreeBSD | Tier 3 upstream. Don't promise. |

## What the guarantee means operationally

The mirror only ships artifacts for the guaranteed + tier-2 list
([mirror-content.md](mirror-content.md)); the install script refuses with a
clear message on anything else rather than half-working. The matrix is
reviewed whenever the pinned `uv` version moves — Astral's platform tiers are
re-checked at that point, and the manifest is the enforcement point (a platform
not in the manifest is not installable).

One nuance: Claude Code / Codex / OpenCode themselves have their own platform
requirements (e.g. Codex stable >= 0.129.0 for `lite configure codex`). The
`lite` matrix above is about getting the CLI onto the machine; the harness
docs each state their own prerequisites, and the CLI checks them
(`lite configure` verifies `codex --version` before touching settings —
verified in the CLI source).
