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
├── jq/<version>/jq-<platform>        # official jqlang static binaries
├── providers/adeo/<harness>/v<N>.json       # per-harness presets (all 7)
└── templates/install.sh.tmpl                 # optional store override
```

Served to clients as `/install/manifest.json`, `/install/cc-switch/...`,
`/install/jq/...`, `/install/providers/adeo/<harness>/v<N>.json`.

## `manifest.json`

Single source of truth for versions and hashes, served at
`/install/manifest.json`. It is fetched by the install script first, and **its
SHA-256 values are the client-side integrity source of truth**: the install
script verifies each downloaded archive against the manifest, not against a
separately-downloaded checksums file. The gateway also reads it to build the
install-script render context.

Schema 5 carries a `platforms` array so the install template can iterate the
target matrix as data, a `cc_switch_artifacts` map of target -> file + sha256,
a `harnesses` map pinning **every** harness to a versioned preset (no mode
field: all harnesses compose the same way), and an optional
`max_output_tokens` integer (default 65536): the per-request output cap the
installer writes into harness configs that have a real output knob (claude
`CLAUDE_CODE_MAX_OUTPUT_TOKENS`, opencode `limit.output`). Changing it is a
manifest edit + re-sync, no gateway rebuild. There is no backwards
compatibility with schema 4: the gateway accepts only `schema: 5`.

**jq lives outside the manifest, pinned in the script itself.** The install
script needs jq to parse the manifest, so the jq version + per-platform
SHA-256 digests cannot be read from the manifest (chicken and egg). They are
literal pins in the template, mirrored by `sync-mirror.sh` from the official
jqlang release (verified against the release's `sha256sum.txt` sidecar), and
served at `/install/jq/<version>/<file>`. The script bootstraps jq from there
when the host has none.

```json
{
  "schema": 5,
  "cc_switch_version": "jya0-5.11.1",
  "max_output_tokens": 65536,
  "platforms": [
    {"target": "linux-x64-musl", "archive_ext": "tar.gz"},
    {"target": "linux-arm64-musl", "archive_ext": "tar.gz"},
    {"target": "darwin-universal", "archive_ext": "tar.gz"},
    {"target": "windows-x64", "archive_ext": "zip"}
  ],
  "cc_switch_artifacts": {
    "linux-x64-musl":  {"file": "cc-switch-cli-jya0-5.11.1-linux-x64-musl.tar.gz",  "sha256": "..."},
    "linux-arm64-musl":{"file": "cc-switch-cli-jya0-5.11.1-linux-arm64-musl.tar.gz","sha256": "..."},
    "darwin-universal":{"file": "cc-switch-cli-jya0-5.11.1-darwin-universal.tar.gz","sha256": "..."},
    "windows-x64":     {"file": "cc-switch-cli-jya0-5.11.1-windows-x64.zip",        "sha256": "..."}
  },
  "harnesses": {
    "claude":    {"version": 2, "path": "providers/adeo/claude/v2.json",    "sha256": "..."},
    "codex":     {"version": 2, "path": "providers/adeo/codex/v2.json",     "sha256": "..."},
    "gemini":    {"version": 2, "path": "providers/adeo/gemini/v2.json",    "sha256": "..."},
    "open-code": {"version": 2, "path": "providers/adeo/open-code/v2.json", "sha256": "..."},
    "pi":        {"version": 2, "path": "providers/adeo/pi/v2.json",        "sha256": "..."},
    "hermes":    {"version": 2, "path": "providers/adeo/hermes/v2.json",    "sha256": "..."},
    "open-claw": {"version": 2, "path": "providers/adeo/open-claw/v2.json", "sha256": "..."}
  }
}
```

Only the artifacts for the [supported matrix](platform-matrix.md) are listed;
the admin sync refuses to publish a manifest referencing a missing object
(binary or preset).

The starting matrix is deliberately small: **Linux x86-64 musl, Linux ARM64
musl, macOS universal, Windows x64**. Musl builds are statically linked, so
they run on any libc and we do not need separate glibc/musl rows; macOS
universal covers both Intel and Apple Silicon. More targets can be added by
appending to `platforms` + `cc_switch_artifacts`.

## cc-switch release artifacts

Source: the ADEO fork's GitHub release (github.com/AITDEV00/cc-switch-cli) for
the pinned version (`jya0-5.11.1` today). The fork carries the
`provider add --overwrite` upsert the installer's same-id refresh depends on;
upstream builds do not have it. Naming follows the release scheme verbatim
(fork tags carry no leading `v`, so neither do the asset names):

```
/install/cc-switch/jya0-5.11.1/cc-switch-cli-jya0-5.11.1-linux-x64-musl.tar.gz
/install/cc-switch/jya0-5.11.1/cc-switch-cli-jya0-5.11.1-linux-arm64-musl.tar.gz
/install/cc-switch/jya0-5.11.1/cc-switch-cli-jya0-5.11.1-darwin-universal.tar.gz
/install/cc-switch/jya0-5.11.1/cc-switch-cli-jya0-5.11.1-windows-x64.zip
```

The musl builds are statically linked, so the binary runs on any libc — that
is why there is no Python/uv/wheelhouse tier at all.

**Integrity chain.** The fork release publishes a `checksums.txt` covering every
asset. At ingest time the admin sync downloads each archive **and** verifies
it against the release's `checksums.txt`; only the verified SHA-256 then goes
into our `manifest.json`. The client never sees upstream's checksums file —
it trusts only the manifest, which is written over the authenticated admin
channel.

**Immutability.** A `cc-switch/<version>/<file>` object, once published with a
given sha256, is never overwritten with different bytes; a new build gets a
new version path. The manifest is published with a generation/ETag so two
concurrent admin syncs cannot silently clobber each other (see
[admin-sync.md](admin-sync.md)).

## Harness presets — `providers/adeo/<harness>/v<N>.json`

Every harness owns a **versioned preset slice**: the canonical cc-switch
`settings_config` skeleton for that harness — the exact shape cc-switch v5.11.1
itself stores — with **sentinels** where runtime values go. Identical for every
user; no per-user data is ever written to the store. Served at
`/install/providers/adeo/<harness>/v<N>.json` (store copy when present, baked
fallback otherwise — `sync-mirror.sh` generates the staged presets from the
gateway's baked set, so the two cannot drift).

The manifest pins each harness to a specific version + sha256, so revising a
harness's config shape is an independent publish: bump the version, push a new
`v<N>.json`, and repoint the manifest — without touching the installer script,
the cc-switch binary, or any other harness.

The per-harness shapes (all verified against the v5.11.1 binary — see
[installer-script.md](installer-script.md) step 4 for the compose). Preset
v2 adds per-model context windows (`context_length` from the key-scoped
`/api/v1/models`) wherever the harness has a real knob, plus the manifest's
`max_output_tokens` default for the two harnesses with an output knob:

| Harness | Sentinels in preset | Notes |
|---|---|---|
| claude | `env` with `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_MODEL` + the three role-model slots, `CLAUDE_CODE_MAX_CONTEXT_TOKENS` (`__DEFAULT_MODEL_CONTEXT__`), `CLAUDE_CODE_MAX_OUTPUT_TOKENS` (`__MAX_OUTPUT_TOKENS__`) | what cc-switch's own field-mode add stores, plus the two managed limit env keys |
| codex | TOML `config` string (with `model_context_window = __DEFAULT_MODEL_CONTEXT__`) + `auth.OPENAI_API_KEY` | cc-switch stores a TOML doc, not env keys; no output knob exists |
| gemini | `env` with `GEMINI_API_KEY`, `GOOGLE_GEMINI_BASE_URL`, `GEMINI_MODEL` | writer drops env keys outside its allow-list, so no limit fields |
| open-code | `npm`, `options.baseURL/apiKey`, `{"__MODELS_OBJECT_LIMIT__": null}` | models as `{id:{name,limit:{context,output}}}` |
| hermes | snake_case `base_url`/`api_key`/`api_mode`, `["__MODELS_ARRAY_CTX_SNAKE__"]` | models as `[{id,name,context_length}]` |
| open-claw | flat `baseUrl`/`apiKey`, `["__MODELS_ARRAY_CTX__"]` | rejects `npm`/`options` as legacy keys; ≥1 model required; per-model `contextWindow` |
| pi | flat `baseUrl` (per-provider), `["__MODELS_ARRAY_CTX__"]` | pi reads the provider-level flat baseUrl; per-model `contextWindow` |

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
