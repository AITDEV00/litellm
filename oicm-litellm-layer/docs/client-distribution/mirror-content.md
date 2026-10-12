# Mirror Content — exact bytes we serve

Everything under `/mirror/` is produced by a sync job (see
[deployment.md](deployment.md)) on a machine that does have internet, then
shipped into the cluster. The dev machine only ever consumes.

## `/mirror/manifest.json`

Single source of truth for versions and hashes. Fetched by the install script
first; every later download is cross-checked against it.

```json
{
  "schema": 1,
  "litellm_cli_version": "1.102.0",
  "uv_version": "0.10.9",
  "python_version": "3.13.9",
  "uv_artifacts": {
    "x86_64-unknown-linux-gnu": {"file": "uv-x86_64-unknown-linux-gnu.tar.gz", "sha256": "..."},
    "x86_64-unknown-linux-musl": {"file": "uv-x86_64-unknown-linux-musl.tar.gz", "sha256": "..."},
    "aarch64-unknown-linux-gnu": {"file": "uv-aarch64-unknown-linux-gnu.tar.gz", "sha256": "..."},
    "aarch64-unknown-linux-musl": {"file": "uv-aarch64-unknown-linux-musl.tar.gz", "sha256": "..."},
    "x86_64-apple-darwin": {"file": "uv-x86_64-apple-darwin.tar.gz", "sha256": "..."},
    "aarch64-apple-darwin": {"file": "uv-aarch64-apple-darwin.tar.gz", "sha256": "..."},
    "x86_64-pc-windows-msvc": {"file": "uv-x86_64-pc-windows-msvc.zip", "sha256": "..."},
    "aarch64-pc-windows-msvc": {"file": "uv-aarch64-pc-windows-msvc.zip", "sha256": "..."}
  },
  "python_artifacts": {
    "sha256": "..."
  },
  "wheelhouse": {
    "index": "pypi/index.html",
    "sha256": "..."
  }
}
```

Only the artifacts for the [supported matrix](platform-matrix.md) are listed;
the sync job refuses to publish a manifest referencing a missing file.

## uv release artifacts

Source: the `astral-sh/uv` GitHub release for the pinned version — exactly the
version upstream's own `scripts/install-cli.sh` pins (`UV_VERSION="0.10.9"`
today), so the CLI and the mirror stay in lockstep with upstream's tested
combination.

Naming follows Astral's release scheme verbatim (verified against the pinned
installer's `select_archive_for_arch`):

```
/mirror/uv/0.10.9/uv-x86_64-unknown-linux-gnu.tar.gz
/mirror/uv/0.10.9/uv-x86_64-unknown-linux-musl.tar.gz
/mirror/uv/0.10.9/uv-aarch64-unknown-linux-gnu.tar.gz
/mirror/uv/0.10.9/uv-aarch64-unknown-linux-musl.tar.gz
/mirror/uv/0.10.9/uv-x86_64-apple-darwin.tar.gz
/mirror/uv/0.10.9/uv-aarch64-apple-darwin.tar.gz
/mirror/uv/0.10.9/uv-x86_64-pc-windows-msvc.zip
/mirror/uv/0.10.9/uv-aarch64-pc-windows-msvc.zip
```

Each `.tar.gz` is served next to its checksum sidecar (Astral publishes
`.sha256` files in the release) at `/mirror/uv/0.10.9/<file>.sha256`, plus the
same hash in `manifest.json`.

The installer's own override hooks (verified in the pinned `install.sh`):
`INSTALLER_DOWNLOAD_URL` replaces the entire artifact download URL list, and
`UV_INSTALLER_GITHUB_BASE_URL` re-points it at a self-hosted mirror of the
release layout. We use `INSTALLER_DOWNLOAD_URL` because we serve a flat
per-file layout rather than a GitHub-release-shaped tree.

## Wheelhouse: `litellm[cli]` + closure

Two options, pick by risk tolerance (decision logged in
[open-questions.md](open-questions.md)):

- **Option A — flat `--find-links` wheelhouse.** One directory of `.whl` files
  with no index metadata at all. Install with
  `uv tool install --no-index --find-links https://.../mirror/pypi 'litellm[cli]'`.
  Fully offline-proof; `--no-index` makes it impossible for uv to quietly
  reach PyPI. Chosen as the default because the failure mode of an accidental
  external hit is the exact thing we are preventing.
- **Option B — PEP 503 simple index.** Let the sync job generate
  `index.html` links pages. Needed only if we ever want uv's resolver to see
  candidate versions beyond the pinned one (e.g. user asks for a specific older
  `lite`). Defer.

Content: the exact version-resolved closure that
`uv tool install 'litellm[cli]'` produces on the sync machine — `litellm`
itself, the `cli` extra's direct dependencies, and every transitive wheel.
Recording the exact closure (not "latest of everything") means the mirror is
byte-for-byte the combination upstream ships for that release. The sync job
runs `uv pip compile` (or `uv tool install` into a scratch venv, then copies
the wheels out of uv's cache) against the target Python version and writes the
resulting file list plus hashes into the manifest.

Requires-python note: upstream pins `requires-python = ">=3.10, <3.15"` and the
installer passes `--python '>=3.10,<3.15' --python-preference system`, so the
wheelhouse must cover the interpreter range, and wheels that publish
multiple ABI variants (e.g. orjson, pydantic-core, tokenizers wheels for
cp310-cp313) must include every variant the sync job resolves for.

Update cadence: per upstream `litellm` release we choose to adopt — the
manifest's `litellm_cli_version` moves, the sync job re-resolves, and old
wheelhouses stay (previous manifest versions kept for rollback).

## Managed Python mirror

`uv` fetches managed CPython from python-build-standalone releases when the
host has no suitable interpreter. Redirect with:

```
UV_PYTHON_INSTALL_MIRROR=https://litellm.ecouncil.ae/mirror/python
```

This is an official uv mechanism (the URL replaces the upstream
`python-build-standalone` download prefix; `file://` also works but our target
machines consume over HTTPS from the same origin). The sync job mirrors the
exact archives for the pinned `python_version` (e.g. `3.13.9`) across the
supported platforms — the same archives the pinned `uv` would fetch, taken
from the python-build-standalone release that `uv` 0.10.9 pins in its own
metadata.

Sizing note: python-build-standalone archives are ~30-70 MB each; the whole
matrix is a few hundred MB. The wheelhouse is ~50-150 MB. The uv artifacts are
~20 MB each. Total artifact store well under a few GB — a PVC or a plain
ConfigMap-free OCI image are both viable (see [deployment.md](deployment.md)).

## What we deliberately do NOT mirror

- Homebrew (`packaging/homebrew/lite.rb`) — a macOS developer behind the
  firewall can use `/install` the same as anyone else; brew taps imply
  external fetches.
- npm, cargo, or any other toolchain — out of scope; the coding harnesses
  themselves (Claude Code, OpenCode, ...) are installed by the user through
  their own channels, and `lite configure` wires them up. Mirroring those
  installers is a possible later extension, explicitly deferred
  ([open-questions.md](open-questions.md)).
