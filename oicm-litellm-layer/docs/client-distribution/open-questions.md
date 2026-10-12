# Open Questions

Decisions that need a human call before implementation. Each has a default so
the plan is actionable without waiting.

## 1. Public path prefix: `/mirror` vs something nicer

`/mirror` is collision-proof but reads as internal. `/client`, `/bootstrap`,
`/dl` are nicer but risk colliding with a future upstream route (upstream has
no such routes today — verified — but the proxy's route surface is huge).

**Default:** `/mirror`. Rename later costs an ingress edit and one script
line.

## 2. Wheelhouse index form: flat find-links vs PEP 503

Flat `--find-links` + `--no-index` is the fail-closed choice (no resolver
freedom, impossible to reach PyPI accidentally). PEP 503 allows asking for
arbitrary versions.

**Default:** flat find-links. PEP 503 only if a real need for
multi-version selection appears.

## 3. Manifest signing

Deferring signature verification (see [security.md](security.md)). The cost
of a full store compromise is currently bounded by the internal-only posture;
the cost of key management now is real.

**Default:** ship unsigned-with-hashes; revisit at broad adoption.

## 4. Windows support timing

No Windows user is known behind the firewall. The design supports it
(`/install.ps1`, x64 + ARM64 artifacts in the matrix) but nothing is built.

**Default:** design documented, implementation deferred until a Windows user
exists.

## 5. Should the harnesses themselves also be mirrored?

Claude Code, OpenCode, Codex each install via npm/curl/brew from their own
origins. `lite configure` wires them up but does not install them. A
hard-locked machine that cannot run `npm install -g` is stuck even with `lite`
working. Mirroring the harness installers is a scope extension of the same
pattern (mirror their artifacts, serve same-origin).

**Default:** out of scope for v1; track as the most likely v2. The pilot will
reveal whether locked-down machines in practice already have these tools or
not.

## 6. Upstream contribution

The `/install` script is a fork of upstream's `scripts/install-cli.sh` with
origin overrides. Upstream might take a PR making the origins configurable
(env vars already cover most of it: `INSTALLER_DOWNLOAD_URL` for uv,
`--find-links/--no-index` for wheels, `UV_PYTHON_INSTALL_MIRROR` for Python).
If so, our script collapses to a thin wrapper and upstream maintains the
installer logic.

**Default:** keep the fork small and local; propose the env-var-parameterized
installer upstream once the pilot proves the design (low priority — the fork
is 3 modifications).

## 7. Adoption cadence for `litellm` releases

The mirror pins one `litellm[cli]` version at a time. Adopting a new upstream
release = sync job re-run + manifest swap. How far behind upstream we are
willing to run (a week? a month? feature-gated?) is a policy call.

**Default:** adopt at most one minor behind latest stable, review CLI-related
upstream changelogs before swapping the manifest.
