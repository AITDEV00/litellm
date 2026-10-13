# Mirror Content — exact bytes we serve

Everything under the mirror is produced by the admin sync script (see
[admin-sync.md](admin-sync.md)) on a machine that does have internet, then
pushed into the cluster over the gateway's admin REST API into the RustFS
bucket `litellm-client-mirror`. The dev machine only ever consumes, through
the gateway.

Storage decision (v4): **RustFS**, a cluster-internal S3-compatible store in
`adeo-litellm`. v3 chose a PVC; v4 moved to object storage because it removes
the multi-replica constraint entirely and because LiteLLM already ships S3
logging support (`integrations/s3.py`, `s3_v2.py`) that can share the same
instance. RustFS replaces the originally sketched MinIO because MinIO
Community Edition reached end of life (repository archived, images deleted
from Docker Hub). See [rustfs.md](rustfs.md).

**One current version of everything.** Per decision 2, the mirror holds exactly
one `uv` version and one `litellm[cli]` version — the ones matching the current
gateway. There is no multi-version selection and no PEP 503 index. One previous
version set is retained only as a race guard: a client that started a download
moments before a version swap should still be able to finish, so the swap keeps
the outgoing set until the incoming one is fully published.

## Object layout in the bucket

```
litellm-client-mirror/
├── manifest.json
├── uv/<version>/uv-<target>.tar.gz (+ .sha256)
├── pypi/<wheel files>            (flat, no index metadata)
└── python/<archive files>
```

Served to clients as `/install/manifest.json`, `/install/uv/...`,
`/install/pypi/...`, `/install/python/...`.

## `manifest.json`

Single source of truth for versions and hashes, served at
`/install/manifest.json`. Fetched by the install script first; every later
download is cross-checked against it. The gateway also reads it to build the
install-script render context.

Schema 2 adds a `platforms` array so the install template can iterate the
target matrix as data instead of hardcoding it in bash. There is no
backwards compatibility with schema 1: the gateway accepts only `schema: 2`.

```json
{
  "schema": 2,
  "litellm_cli_version": "1.102.0",
  "uv_version": "0.10.9",
  "python_version": "3.13.9",
  "platforms": [
    {"target": "x86_64-unknown-linux-gnu", "archive_ext": "tar.gz"},
    {"target": "x86_64-unknown-linux-musl", "archive_ext": "tar.gz"},
    {"target": "aarch64-unknown-linux-gnu", "archive_ext": "tar.gz"},
    {"target": "aarch64-unknown-linux-musl", "archive_ext": "tar.gz"},
    {"target": "x86_64-apple-darwin", "archive_ext": "tar.gz"},
    {"target": "aarch64-apple-darwin", "archive_ext": "tar.gz"},
    {"target": "x86_64-pc-windows-msvc", "archive_ext": "zip"},
    {"target": "aarch64-pc-windows-msvc", "archive_ext": "zip"}
  ],
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
    "sha256": "..."
  }
}
```

Only the artifacts for the [supported matrix](platform-matrix.md) are listed;
the admin sync refuses to publish a manifest referencing a missing object.

## uv release artifacts

Source: the `astral-sh/uv` GitHub release for the pinned version — exactly the
version upstream's own `scripts/install-cli.sh` pins (`UV_VERSION="0.10.9"`
today), so the CLI and the mirror stay in lockstep with upstream's tested
combination.

Naming follows Astral's release scheme verbatim (verified against the pinned
installer's `select_archive_for_arch`):

```
/install/uv/0.10.9/uv-x86_64-unknown-linux-gnu.tar.gz
/install/uv/0.10.9/uv-x86_64-unknown-linux-musl.tar.gz
/install/uv/0.10.9/uv-aarch64-unknown-linux-gnu.tar.gz
/install/uv/0.10.9/uv-aarch64-unknown-linux-musl.tar.gz
/install/uv/0.10.9/uv-x86_64-apple-darwin.tar.gz
/install/uv/0.10.9/uv-aarch64-apple-darwin.tar.gz
/install/uv/0.10.9/uv-x86_64-pc-windows-msvc.zip
/install/uv/0.10.9/uv-aarch64-pc-windows-msvc.zip
```

Each `.tar.gz` is served next to its checksum sidecar (Astral publishes
`.sha256` files in the release) at `/install/uv/0.10.9/<file>.sha256`, plus the
same hash in `manifest.json`.

The installer's own override hooks (verified in the pinned `install.sh`):
`INSTALLER_DOWNLOAD_URL` replaces the entire artifact download URL list, and
`UV_INSTALLER_GITHUB_BASE_URL` re-points it at a self-hosted mirror of the
release layout. We use `INSTALLER_DOWNLOAD_URL` because we serve a flat
per-file layout rather than a GitHub-release-shaped tree.

## Wheelhouse: `litellm[cli]` + closure

**One form: flat `--find-links`.** Per decision 2 (one current version of
everything), there is no PEP 503 index and no multi-version selection. The
bucket holds one flat directory of `.whl` files with no index metadata:

```
uv tool install --no-index --find-links https://.../install/pypi 'litellm[cli]'
```

`--no-index` makes it impossible for uv to quietly reach PyPI; if the
wheelhouse is missing something it fails loudly instead of phoning home. That
is the whole point, so this is settled, not a choice.

Content: the exact version-resolved closure that
`uv tool install 'litellm[cli]'` produces on the sync machine — `litellm`
itself, the `cli` extra's direct dependencies, and every transitive wheel.
Recording the exact closure (not "latest of everything") means the mirror is
byte-for-byte the combination upstream ships for that release. The sync script
runs `uv pip compile` (or `uv tool install` into a scratch venv, then copies
the wheels out of uv's cache) against the target Python version and writes the
resulting file list plus hashes into the manifest.

Requires-python note: upstream pins `requires-python = ">=3.10, <3.15"` and the
installer passes `--python '>=3.10,<3.15' --python-preference system`, so the
wheelhouse must cover the interpreter range, and wheels that publish
multiple ABI variants (e.g. orjson, pydantic-core, tokenizers wheels for
cp310-cp313) must include every variant the sync job resolves for.

Update cadence (decision 7): the pinned `litellm[cli]` version always equals
the gateway's version, so every gateway bump re-pins the CLI. The admin sync
publishes the new set, then removes the outgoing set once the new manifest is
live (the one-previous retention from the race guard above).

## Managed Python mirror

`uv` fetches managed CPython from python-build-standalone releases when the
host has no suitable interpreter. Redirect with:

```
UV_PYTHON_INSTALL_MIRROR=https://litellm.ecouncil.ae/install/python
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
~20 MB each. Total well under a few GB — comfortable in a RustFS bucket (see
[rustfs.md](rustfs.md)).

## What we deliberately do NOT mirror

- Homebrew (`packaging/homebrew/lite.rb`) — a macOS developer behind the
  firewall can use `/install` the same as anyone else; brew taps imply
  external fetches.
- npm, cargo, or any other toolchain — out of scope; the coding harnesses
  themselves (Claude Code, OpenCode, ...) are installed by the user through
  their own channels, and `lite configure` wires them up. Mirroring those
  installers is a possible later extension, explicitly deferred
  ([open-questions.md](open-questions.md)).
