# The Served Install Script (`/install`)

Design rule: copy upstream, change origins, add nothing. The script is a
lightly-modified fork of upstream `scripts/install-cli.sh` (2139-line uv
installer aside, the LiteLLM part is ~120 lines). Keeping the diff tiny keeps
the maintenance burden proportional: when upstream changes its installer, we
re-apply three modifications, not re-invent.

In v2 the script is not a static file: the gateway slice generates it at
request time, filling the gateway base URL and the adopted versions from
`manifest.json` (see [architecture.md](architecture.md), "Why `/install` is
generated"). The template below is what the generator emits.

The script is served at `GET /install` and is **unauthenticated** (decision 9):
installing the CLI and wiring a harness needs no credential. The user supplies
their LiteLLM key later, at `lite login` / `lite configure`, which is where it
belongs. See [security.md](security.md).

## The three modifications

### 1. uv bootstrap -> gateway mirror

Upstream (verified, `scripts/install-cli.sh` at the pinned uv):

```sh
curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | env UV_NO_MODIFY_PATH=1 sh
```

Ours:

```sh
export INSTALLER_DOWNLOAD_URL="https://litellm.ecouncil.ae/install/uv/${UV_VERSION}"
curl -fsSL "https://litellm.ecouncil.ae/install/uv/${UV_VERSION}/uv-installer.sh" \
  | env INSTALLER_DOWNLOAD_URL="${INSTALLER_DOWNLOAD_URL}" UV_NO_MODIFY_PATH=1 sh
```

Two details verified in the pinned installer source: the installer script
itself is static content (we mirror it like any other artifact), and
`INSTALLER_DOWNLOAD_URL` makes the installer take artifact URLs from our
mirror prefix instead of `github.com/astral-sh/uv/releases/download/...`. The
musl/gnu/libc detection, archive extraction, PATH handling, and receipt logic
all stay Astral's, unmodified — that is the mature platform logic we do not
want to rewrite.

### 2. `uv tool install` -> offline wheelhouse

Upstream:

```sh
"$UV_BIN" tool install --python '>=3.10,<3.15' --python-preference system --force 'litellm[cli]'
```

Ours:

```sh
export UV_PYTHON_INSTALL_MIRROR="https://litellm.ecouncil.ae/install/python"
"$UV_BIN" tool install \
  --python '>=3.10,<3.15' --python-preference system --force \
  --no-index --find-links "https://litellm.ecouncil.ae/install/pypi" \
  'litellm[cli]'
```

Verified: `uv tool install` accepts `--no-index` and `--find-links` (and
`UV_INDEX_URL`/`UV_EXTRA_INDEX_URL` env equivalents). `--no-index` is the
guarantee that no resolution step can escape to PyPI even if the wheelhouse
is missing something — it fails loudly instead of silently phoning home.
`UV_PYTHON_INSTALL_MIRROR` covers the managed-Python fetch on hosts with no
compatible interpreter.

### 3. Gateway pre-configuration

After a successful install, the script appends the one thing that makes the
CLI usable out of the box (and nothing else):

```sh
"$LITE_BIN" config set base_url https://litellm.ecouncil.ae
```

Verified: `lite config set base_url <url>` persists to `~/.litellm/config.json`
(`normalize_base_url` rejects credentials, whitespace, fragments — so the
script cannot be tricked into writing a hostile base_url), and every later
command reads it as the default. The user's API key is NOT in the script; it
is supplied at first `lite login` / `--api-key` / `LITELLM_PROXY_API_KEY`, and
the stock flow takes over (key check against `/v1/models`, harness config,
model sync).

Not added, deliberately: no version checks beyond the manifest, no harness
installation (Claude Code / OpenCode are installed by their own channels),
no auto-run of `lite login` (it is interactive on first use; the script must
stay non-interactive so `curl | sh` cannot hang).

## Generator contract (v2 addition)

The slice's `/install` handler owns these substitutions — nothing else
varies:

| Placeholder | Source |
|---|---|
| `{{BASE_URL}}` | `LITELLM_PUBLIC_BASE_URL` env/config, request host as fallback |
| `{{UV_VERSION}}` | `manifest.json` `uv_version` |
| `{{LITELLM_CLI_VERSION}}` | `manifest.json` `litellm_cli_version` (informational banner only) |

The generator is pure (manifest + base URL in, script text out) and unit
tested: render with a known manifest, assert the three origin overrides and
the `lite config set` line appear with the right values. The test belongs in
the slice's co-located test module, in the established drop-detection style.

## Harness detection (v3 addition)

After installing `lite`, the script scans the machine for known coding
harnesses and **reports** what it found. It does not auto-select one; choosing
which harness to wire is left to the user (a deliberate product decision —
`lite configure` and the per-agent commands already handle selection).

The scan is cross-platform and cheap:

| Platform | Probe |
|---|---|
| POSIX (Linux/macOS) | `command -v claude`, `command -v codex`, `command -v opencode`, `command -v pi` |
| Windows | `where claude`, etc. (same names) |

The script does **not** reimplement the Windows `.cmd`/`.bat` shim logic; that
is already handled inside `lite` itself (`_WINDOWS_SHIM_SUFFIXES` /
`_windows_command` in `agents.py`), and the script's job is only to report.
The authoritative detection happens at run time when `lite <agent>` resolves
the binary with `shutil.which` and raises a clear "could not find" with an
install-docs hint.

Output is a plain summary so the user knows what `lite configure` / `lite
<agent>` will work with:

```
Detected harnesses:
  claude    found   (Claude Code)
  codex     found   (Codex)
  opencode  not found
  pi        not found
Configure a found harness with:  lite configure claude
```

Two caveats recorded, not solved:

- The `lite configure` wizard's target list is Claude Code + Codex only
  (`_TARGETS` in `configure.py`); OpenCode and pi are wired by their own
  commands (`lite opencode`, `lite pi`). So the summary line should point at
  the right command per harness, not always `lite configure`.
- On a platform `uv` does not support as a host (Android/Termux experimental,
  iOS unsupported — see [platform-matrix.md](platform-matrix.md)), the script
  fails with a clear message before the scan rather than producing a summary
  for a machine it cannot install onto.

## Resume behaviour (v3 addition)

A wrong API key at the auth step must never cost a re-download. The script is
two independently re-runnable phases, with progress recorded so a second run
skips what is already done.

```
Phase A — acquire (expensive; touches the gateway mirror)
  A1 uv present and version matches manifest   -> skip
  A2 lite present and version matches manifest -> skip
  A3 base_url already set to this gateway      -> skip
  (progress: ~/.litellm/install_state.json)

Phase B — auth + configure (cheap; user-supplied values)
  B1 lite login / --api-key   <- a wrong key lands here and is retryable
  B2 lite configure <harness> (or lite <agent>)
```

Properties:

- Re-running `/install` after a bad key re-enters at Phase B; A1-A3
  short-circuit on the state file plus the manifest version check, so nothing
  is downloaded again.
- Each phase is idempotent on its own, so a partial run (network drop
  mid-download) resumes cleanly: `uv tool install --force` handles a
  half-written tool, and the version check makes "already correct" a no-op.
- The state file records only phase completion and the adopted version, never
  the key. The key stays in the OS keyring or the CLI's own config, where it
  already lives.
- Phase B can be interactive (`lite login` prompts) or, in a fully scripted
  run, supplied `--api-key`; either way it is a distinct step, so a piped
  `curl | sh` never blocks on the interactive part unless the user chose to
  run Phase B in that same invocation.

This closes a gap in the earlier draft, which said re-running was safe and
idempotent but did not separate acquire from auth or record progress.

## Idempotency and update path

Re-running `/install` must be safe and must be the update mechanism:

1. Script fetches `/install/manifest.json` first.
2. If `lite` exists and reports the manifest's `litellm_cli_version`, exit 0
   with a "already up to date" line (no reinstall).
3. Otherwise proceed; `uv tool install --force` replaces the tool in place.
   `lite`'s own state (`~/.litellm/config.json`, keyring tokens) survives
   reinstalls — verified: those live under `~/.litellm/` and the OS keyring,
   not inside the tool venv.

CLI version != model access: adding/removing a model on the gateway never
requires a reinstall — `/v1/models` is read live by the CLI at
launch/configure time. This preserves the clean separation (distribution vs
entitlement).

Version policy (decision 7): the mirror pins exactly one `litellm[cli]`
version, always equal to the gateway's version. A gateway bump re-pins the CLI
and the admin publishes the new set; the outgoing set is retained only until
the new manifest is live, to cover a client mid-download during the swap.

## Windows (Git Bash / WSL)

A bash script cannot run natively in PowerShell, so native `/install.ps1` is
deferred (decision 4). Windows is supported today through the shells that
already ship bash:

- **Git Bash** (ships with Git for Windows) and **WSL** both run the script
  unchanged.
- One detection detail: under Git Bash / MSYS, `uname -s` returns
  `MINGW64_NT-...` (or `MSYS_NT-...`), not `Linux`. The script maps a
  `MINGW*` / `MSYS*` result to the Windows artifacts
  (`x86_64-pc-windows-msvc` / `aarch64-pc-windows-msvc`) so the platform
  detection still lands on the right tarball.
- Harness detection uses `where <name>` under these shells, as in the table
  above.

## Failure modes the script must handle

- Mirror missing a platform artifact -> clear message naming the exact URL
  that 404'd (the manifest lists what should exist; the script trusts nothing
  not in the manifest).
- Checksum mismatch -> abort before extraction/installation, message says
  "artifact store may be corrupt, contact platform team".
- `lite` present but broken (half-installed tool) -> `--force` re-install
  handles it; no special-casing.
- No `curl` -> the script cannot bootstrap; error says so (acceptable: every
  target OS in the matrix ships curl or wget; wget fallback is a two-line
  mapping of the same URLs and can be added if a real user needs it).
- MinIO unreachable (the gateway proxies reads from it) -> the artifact fetch
  returns 503 from the slice; the script aborts with the URL, never falls back
  to the internet.
