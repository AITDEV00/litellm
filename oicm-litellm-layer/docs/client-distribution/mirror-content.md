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

**One current version of everything.** The mirror holds exactly one
cc-switch version. One previous version set is retained only as a race guard:
a client that started a download moments before a version swap should still be
able to finish, so the swap keeps the outgoing set until the incoming one is
fully published.

## Object layout in the bucket

```
litellm-client-mirror/
├── manifest.json
├── cc-switch/<version>/cc-switch-cli-<target>.<ext>
├── providers/oicm/<harness>/v<N>.json     # additive harness presets
└── templates/install.sh.tmpl               # optional store override
```

Served to clients as `/install/manifest.json`, `/install/cc-switch/...`,
`/install/providers/oicm/<harness>/v<N>.json`.

## `manifest.json`

Single source of truth for versions and hashes, served at
`/install/manifest.json`. It is fetched by the install script first, and **its
SHA-256 values are the client-side integrity source of truth**: the install
script verifies each downloaded archive against the manifest, not against a
separately-downloaded checksums file. The gateway also reads it to build the
install-script render context.

Schema 4 carries a `platforms` array so the install template can iterate the
target matrix as data, a `cc_switch_artifacts` map of target -> file + sha256,
and a `harnesses` map pinning each harness to a versioned preset. There is no
backwards compatibility with schema 3: the gateway accepts only `schema: 4`.

```json
{
  "schema": 4,
  "cc_switch_version": "5.11.1",
  "platforms": [
    {"target": "linux-x64-musl", "archive_ext": "tar.gz"},
    {"target": "linux-arm64-musl", "archive_ext": "tar.gz"},
    {"target": "darwin-universal", "archive_ext": "tar.gz"},
    {"target": "windows-x64", "archive_ext": "zip"}
  ],
  "cc_switch_artifacts": {
    "linux-x64-musl":  {"file": "cc-switch-cli-v5.11.1-linux-x64-musl.tar.gz",  "sha256": "..."},
    "linux-arm64-musl":{"file": "cc-switch-cli-v5.11.1-linux-arm64-musl.tar.gz","sha256": "..."},
    "darwin-universal":{"file": "cc-switch-cli-v5.11.1-darwin-universal.tar.gz","sha256": "..."},
    "windows-x64":     {"file": "cc-switch-cli-v5.11.1-windows-x64.zip",        "sha256": "..."}
  },
  "harnesses": {
    "claude":    {"version": 1, "mode": "switch"},
    "codex":     {"version": 1, "mode": "switch"},
    "gemini":    {"version": 1, "mode": "switch"},
    "open-code": {"version": 1, "path": "providers/oicm/open-code/v1.json", "sha256": "...", "mode": "additive"},
    "pi":        {"version": 1, "path": "providers/oicm/pi/v1.json",        "sha256": "...", "mode": "additive"},
    "hermes":    {"version": 1, "path": "providers/oicm/hermes/v1.json",    "sha256": "...", "mode": "additive"},
    "open-claw": {"version": 1, "path": "providers/oicm/open-claw/v1.json", "sha256": "...", "mode": "additive"}
  }
}
```

Only the artifacts for the [supported matrix](platform-matrix.md) are listed;
the admin sync refuses to publish a manifest referencing a missing object
(binary or additive-harness preset).

The starting matrix is deliberately small: **Linux x86-64 musl, Linux ARM64
musl, macOS universal, Windows x64**. Musl builds are statically linked, so
they run on any libc and we do not need separate glibc/musl rows; macOS
universal covers both Intel and Apple Silicon. More targets can be added by
appending to `platforms` + `cc_switch_artifacts`.

## cc-switch release artifacts

Source: the `SaladDay/cc-switch-cli` GitHub release for the pinned version
(`5.11.1` today). Naming follows the release scheme verbatim:

```
/install/cc-switch/5.11.1/cc-switch-cli-linux-x64-musl.tar.gz
/install/cc-switch/5.11.1/cc-switch-cli-linux-arm64-musl.tar.gz
/install/cc-switch/5.11.1/cc-switch-cli-darwin-universal.tar.gz
/install/cc-switch/5.11.1/cc-switch-cli-windows-x64.zip
```

The musl builds are statically linked, so the binary runs on any libc — that
is why there is no Python/uv/wheelhouse tier at all.

**Integrity chain.** Upstream publishes a `checksums.txt` covering every
asset. At ingest time the admin sync downloads each archive **and** verifies
it against upstream's `checksums.txt`; only the verified SHA-256 then goes
into our `manifest.json`. The client never sees upstream's checksums file —
it trusts only the manifest, which is written over the authenticated admin
channel.

**Immutability.** A `cc-switch/<version>/<file>` object, once published with a
given sha256, is never overwritten with different bytes; a new build gets a
new version path. The manifest is published with a generation/ETag so two
concurrent admin syncs cannot silently clobber each other (see
[admin-sync.md](admin-sync.md)).

## Harness presets — `providers/oicm/<harness>/v<N>.json`

Each additive harness owns a **versioned preset slice**: the structural
cc-switch JSON skeleton for that harness, identical for every user. The preset
holds structure only; the installer fills runtime values (base URL, API key,
accessible models) client-side with `jq`, so no per-user data is ever written
to the store. Served at `/install/providers/oicm/<harness>/v<N>.json` (store
copy when present, baked fallback otherwise).

The manifest pins each harness to a specific version + sha256, so revising a
harness's config shape is an independent publish: bump the version, push a new
`v<N>.json`, and repoint the manifest — without touching the installer script,
the cc-switch binary, or any other harness.

```json
{
  "npm": "@ai-sdk/openai-compatible",
  "name": "OICM LiteLLM",
  "options": {"baseURL": "", "apiKey": ""},
  "models": {}
}
```

claude/codex/gemini use cc-switch's field-mode flags (`mode: "switch"`) and
carry no preset file; the additive harnesses use `--config-file`
(`mode: "additive"`). See [installer-script.md](installer-script.md) step 4.

Sizing note: each cc-switch archive is ~8-9 MB; the four-target matrix is
~35 MB. Far smaller than the retired uv + wheelhouse + python tiers.

## What we deliberately do NOT mirror

- Homebrew (`packaging/homebrew/lite.rb`) — a macOS developer behind the
  firewall can use `/install` the same as anyone else; brew taps imply
  external fetches.
- npm, cargo, or any other toolchain — out of scope; the coding harnesses
  themselves (Claude Code, OpenCode, ...) are installed by the user through
  their own channels, and cc-switch writes their config. Mirroring those
  installers is a possible later extension, explicitly deferred
  ([open-questions.md](open-questions.md)).
