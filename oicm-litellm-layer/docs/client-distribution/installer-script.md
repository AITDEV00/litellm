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

## The three modifications

### 1. uv bootstrap -> gateway mirror

Upstream (verified, `scripts/install-cli.sh` at the pinned uv):

```sh
curl -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" | env UV_NO_MODIFY_PATH=1 sh
```

Ours:

```sh
export INSTALLER_DOWNLOAD_URL="https://litellm.ecouncil.ae/mirror/uv/${UV_VERSION}"
curl -fsSL "https://litellm.ecouncil.ae/mirror/installers/uv-${UV_VERSION}.sh" \
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
export UV_PYTHON_INSTALL_MIRROR="https://litellm.ecouncil.ae/mirror/python"
"$UV_BIN" tool install \
  --python '>=3.10,<3.15' --python-preference system --force \
  --no-index --find-links "https://litellm.ecouncil.ae/mirror/pypi" \
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

## Idempotency and update path

Re-running `/install` must be safe and must be the update mechanism:

1. Script fetches `/mirror/manifest.json` first.
2. If `lite` exists and reports the manifest's `litellm_cli_version`, exit 0
   with a "already up to date" line (no reinstall).
3. Otherwise proceed; `uv tool install --force` replaces the tool in place.
   `lite`'s own state (`~/.litellm/config.json`, keyring tokens) survives
   reinstalls — verified: those live under `~/.litellm/` and the OS keyring,
   not inside the tool venv.

CLI version != model access: adding/removing a model on the gateway never
requires a reinstall — `/v1/models` is read live by the CLI at launch/configure
time. This preserves the clean separation (distribution vs. entitlement).

## Windows (`/install.ps1`)

Same flow, PowerShell: `irm https://litellm.ecouncil.ae/install.ps1 | iex`.
Astral's `install.ps1` accepts an equivalent environment override for its
artifacts. Not designed further until a Windows user actually exists behind
the firewall ([open-questions.md](open-questions.md)).

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
- Gateway serving but artifact volume not mounted (slice's base dir absent)
  -> the manifest fetch 404s with the slice's explanatory message; the script
  aborts with the URL, never falls back to the internet.
