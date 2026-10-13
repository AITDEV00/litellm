# The Served Install Script (`/install`)

Design rule: copy upstream, change origins, add nothing. The script is a
lightly-modified fork of upstream `scripts/install-cli.sh` (2139-line uv
installer aside, the LiteLLM part is ~120 lines). Keeping the diff tiny keeps
the maintenance burden proportional: when upstream changes its installer, we
re-apply three modifications, not re-invent.

The script is not a static file: the gateway slice renders it at request time
from a Jinja2 template and a structured context (base URL, adopted versions,
platform list) built from `manifest.json` (see
[architecture.md](architecture.md), "Why `/install` is generated", and the
generator contract below). The template below is what the generator emits.

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

## Generator contract (v5: Jinja2, structured context)

The slice's `/install` handler renders the template with **Jinja2**. The
template is a Jinja2 document, not a find-replace string, so it can grow
conditionals and loops without the renderer changing.

The render context is structured (`generator.RenderContext`):

| Variable | Type | Source |
|---|---|---|
| `base_url` | str | `LITELLM_PUBLIC_BASE_URL` env/config, request host as fallback |
| `uv_version` | str | `manifest.json` `uv_version` |
| `litellm_cli_version` | str | `manifest.json` `litellm_cli_version` |
| `python_version` | str | `manifest.json` `python_version` |
| `platforms` | list of `{target, archive_ext}` | `manifest.json` `platforms` |

Rendering is sandboxed, strict, and not autoescaped
(`SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)`):

- **`StrictUndefined`** — a template that references a variable we did not
  pass raises at render time. `/install` returns 500 with the error rather
  than serving a half-rendered installer to every client.
- **`SandboxedEnvironment`** — bounds what a store-pushed template can reach
  (the store copy at `templates/install.sh.tmpl` is content we execute).
- **`autoescape=False`** — shell metacharacters (`&`, `<`, `>`) are not turned
  into HTML entities.

The generator is pure (manifest + base URL in, script text out) and unit
tested in the slice's co-located module: the three origin overrides, the
no-HTML-escape rule, the undefined-variable error, and a `{% for %}` loop over
`platforms` all have regression tests.

A store-pushed template must be valid Jinja2 and reference only context
variables. A bad push breaks `/install` loudly on the next request (500 in the
gateway logs / a curl), so template pushes must be validated before going
live — `sync-mirror.sh --push-only` after editing `install.sh.tmpl`.

## Harness detection — removed (v5)

Earlier versions had the script scan for coding harnesses (`claude`, `codex`,
`opencode`, `pi`) and print a "Detected harnesses" summary. This was removed.

The scan was report-only UX: it did no wiring, and the authoritative detection
already lives in the `lite` CLI (`shutil.which` at runtime, `_TARGETS` in
`configure.py`, and the per-agent `lite <agent>` commands). Keeping a second
hardcoded list in the served script duplicated that state. The "Next steps"
footer still tells the user to run `lite login` / `lite configure claude`,
which is where selection and detection actually happen.

## Resume behaviour (v5: simplified)

A wrong API key at the auth step must never cost a re-download. Re-running is
safe and cheap, with no client-side state file.

The earlier design kept a `~/.litellm/install_state.json` state machine
(`NEED_UV` / `NEED_LITE` / `NEED_CONFIG`) and parsed it with `grep`. That was
over-engineered: the two expensive steps are already idempotent on their own,
so the script now short-circuits on a plain version check and lets the tools
do the rest.

```
A1 uv:        skip if `uv --version` already matches the manifest
A2 lite:      skip if `lite --version` already matches, else
              `uv tool install --force` (idempotent, replaces a half-install)
A3 base_url:  always `lite config set base_url` (idempotent)
```

Properties:

- Re-running after a bad key re-runs only the cheap auth/configure steps; the
  version checks make A1/A2 no-ops, so nothing is downloaded again.
- No `install_state.json`, no JSON parsing in bash. `lite`'s own state
  (`~/.litellm/config.json`, keyring) survives reinstalls because it lives
  outside the tool venv.
- The script stays non-interactive; `lite login` is a separate, later step.

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
- RustFS unreachable (the gateway proxies reads from it) -> the artifact fetch
  returns 503 from the slice; the script aborts with the URL, never falls back
  to the internet.
