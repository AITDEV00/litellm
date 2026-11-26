# Supported Platform Matrix

cc-switch ships static binaries, so the matrix is small. We guarantee the
targets below and label everything else honestly.

## Guaranteed (the mirror ships these)

| Target | Archive | Covers |
|---|---|---|
| Linux x86-64 musl | `cc-switch-cli-linux-x64-musl.tar.gz` | any Linux x86-64, glibc or musl (static) |
| Linux ARM64 musl | `cc-switch-cli-linux-arm64-musl.tar.gz` | any Linux ARM64 |
| macOS universal | `cc-switch-cli-darwin-universal.tar.gz` | Intel + Apple Silicon in one archive |
| Windows x64 | `cc-switch-cli-windows-x64.zip` | via Git Bash / WSL |

Static musl builds mean we do not need separate glibc/musl rows, and the macOS
universal archive collapses the two Apple arches. Four artifacts cover the
whole supported set.

## Supported on request (add to the manifest when needed)

| Target | Archive | Notes |
|---|---|---|
| Linux x86-64 glibc | `cc-switch-cli-linux-x64.tar.gz` | only if a static-musl issue ever shows up |
| Linux ARM64 glibc | `cc-switch-cli-linux-arm64.tar.gz` | same |
| macOS arch-specific | `cc-switch-cli-darwin-{x64,arm64}.tar.gz` | smaller than universal; rarely needed |

## Explicitly not promised

| Environment | Why |
|---|---|
| Windows ARM64 | no upstream cc-switch artifact |
| Android / Termux | no upstream cc-switch artifact |
| iOS / iPadOS | not a sensible local CLI host; SSH into a Linux box instead |
| FreeBSD | no upstream cc-switch artifact |

## What the guarantee means operationally

The mirror only ships artifacts for the guaranteed list
([mirror-content.md](mirror-content.md)); the install script refuses with a
clear message on anything else rather than half-working. The manifest is the
enforcement point: a platform not in `cc_switch_artifacts` is not installable.
The matrix is re-checked whenever the pinned `cc_switch_version` moves.

The coding harnesses themselves (Claude Code, Codex, OpenCode, ...) are
installed by the user through their own channels and have their own platform
requirements; cc-switch only writes their config, it does not install them.
