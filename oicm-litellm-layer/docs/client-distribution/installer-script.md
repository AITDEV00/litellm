# The Served Install Script (`/install`)

Design rule: download one static binary, configure the user's harnesses with
it, add nothing. The script installs **cc-switch-cli** (a single static Rust
binary) from the gateway mirror and uses it to register our gateway as a
provider and write the user's chosen harness config files. There is no uv,
no Python, no wheelhouse, and no `lite` CLI.

The end user already holds an admin-issued gateway key, so there is no login
step. cc-switch supports 7 harnesses (claude, codex, gemini, open-code,
hermes, open-claw, pi) and writes each one's native config file — including a
real `~/.config/opencode/opencode.json`, which the retired `lite` CLI never
did.

The script is not a static file: the gateway slice renders it at request time
from a Jinja2 template and a structured context (base URL, adopted versions,
platform list) built from `manifest.json` (see
[architecture.md](architecture.md) and the generator contract below).

The script is served at `GET /install` and is **unauthenticated**: downloading
the binary and reading the manifest need no credential. The user's key is
prompted for interactively and used only for the `/api/v1/models` diagnostic
and the provider's `apiKey` field. See [security.md](security.md).

The steps below double as **first install** and **reconciliation**: the
same `/install` run installs the binary if absent, then re-validates the key
and re-syncs the provider + model list. One command, both jobs.

UX order: the key and harness prompts run **first**, so a bad key or a wrong
pick fails fast before any download starts; the jq bootstrap, cc-switch
install, and per-harness configure all run after the user has answered.

### 1. Prompt for the gateway key + diagnostic

The script prompts for the user's **gateway virtual key** (visible input, so
a pasted key can be checked before Enter) and validates it before anything is
installed. This is a per-developer virtual key, never the proxy master/admin
key:

```sh
curl -fsSL -H "Authorization: Bearer $API_KEY" "$BASE_URL/api/v1/models?limit=2000" \
  > "$TMP/models.json" \
  || die "that key was rejected by $BASE_URL; check it with your platform admin"
```

A wrong key fails here, before any download begins and before any harness
file is written. The endpoint is
the OpenRouter-compatible models API: it is **key-scoped** (each user sees
exactly their accessible models) and every entry carries its real
`context_length` from live backend discovery, which feeds step 4.

The script filters the fetched list down to text-generation models by
excluding utility families by name (embeddings, speech/STT/TTS/voice, OCR,
transcribe, image generation, rerank, doc-layout, whisper) and UUID-named
document-processing models. The payload carries no capability signal, so a
name-based exclusion is the only available filter; it keeps every real
coding model (GLM, Qwen, Kimi, DeepSeek, ...) rather than the old
include-list, which silently dropped whole families.

### 2. Harness selection

The script lists the harnesses cc-switch can configure and asks the user
which to wire (numbered multi-select). Harnesses not picked are left alone —
the user may already have a non-gateway config for one of them. The list is
baked into the script at render time (the menu must appear before jq is
bootstrapped, so it cannot parse the fetched manifest); it is sorted
alphabetically so menu numbers stay stable across syncs, and adding a
harness remains a store sync, not a script change.

### 3. Install: jq bootstrap, cc-switch

jq and cc-switch are installed from the gateway mirror only after the prompts
are answered, so there is no download wait before the user has said anything.
Both are sha256-verified against the manifest, the client-side source of
truth for integrity (admin sync verifies upstream's `checksums.txt` at ingest
time and records the verified hash in the manifest; see
[admin-sync.md](admin-sync.md)):

```sh
VER="$CC_SWITCH_VERSION"   # rendered from the manifest
FILE="$(jq -r --arg t "$TARGET" '.cc_switch_artifacts[$t].file' "$TMP/manifest.json")"
WANT="$(jq -r --arg t "$TARGET" '.cc_switch_artifacts[$t].sha256' "$TMP/manifest.json")"
curl -fsSL "$BASE_URL/install/cc-switch/$VER/$FILE" -o "$TMP/ccs.tgz"
echo "$WANT  $TMP/ccs.tgz" | sha256sum -c -
tar -xzf "$TMP/ccs.tgz" -C "$INSTALL_DIR"
```

The script detects OS/arch/libc and downloads the matching archive from the
**immutable versioned** path `/install/cc-switch/<version>/`. Musl builds
(`linux-x64-musl`, `linux-arm64-musl`) are statically linked, so the binary
runs on any libc. That is why there is no Python or uv tier at all. If
`cc-switch --version` already matches `cc_switch_version`, the download is
skipped. jq follows the same versioned-mirror flow under
`/install/jq/<version>/` (pins live in the script itself: reading them from
the manifest would already require jq).

### 4. Per-harness provider reconcile

The script is a **generic orchestrator**: it embeds no per-harness JSON shape.
For each chosen harness it reads the pinned preset (`path`, `sha256`) from
the manifest's `harnesses` map, fetches `/install/providers/adeo/<h>/v<N>.json`,
verifies its sha256 against the manifest, then composes the final config with
one generic `jq walk()`: sentinels in the preset
(`__BASE_URL__`, `__BASE_URL_V1__`, `__API_KEY__`, `__DEFAULT_MODEL__`,
`__DEFAULT_MODEL_CONTEXT__`, `__MAX_OUTPUT_TOKENS`, the models placeholders
below) are replaced with the gateway URL, the key, the key's model list,
**each model's real context window** from step 2, and the manifest's default
output cap. Every harness — claude, codex, gemini included — goes through the
same `provider add --config-file` path; the config shape is entirely the
preset's business, so revising one harness touches nothing else.

Per-model context lands wherever the harness has a real, verified knob; the
preset picks the placeholder matching its shape:

| Placeholder | Composes to | Used by |
|---|---|---|
| `["__MODELS_ARRAY__"]` | `[{id,name}]` (v1 presets) | — |
| `["__MODELS_ARRAY_CTX__"]` | `[{id,name,contextWindow?}]` | pi, open-claw |
| `["__MODELS_ARRAY_CTX_SNAKE__"]` | `[{id,name,context_length?}]` | hermes |
| `{"__MODELS_OBJECT__": null}` | `{id:{name}}` (v1 presets) | — |
| `{"__MODELS_OBJECT_LIMIT__": null}` | `{id:{name,limit:{context?,output}}}` | open-code |

A model whose context the gateway does not report simply omits the context
key; for the single-model harnesses the compose drops the whole field: claude
loses `CLAUDE_CODE_MAX_CONTEXT_TOKENS`, codex loses the entire
`model_context_window` TOML line (a first-class supported state — cc-switch
itself defaults the window to 128000 when the key is absent).

**Output cap.** The manifest carries `max_output_tokens` (default 65536, i.e.
64K). It is written into every harness that has a real output knob: claude's
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` env key and opencode's per-model
`limit.output`. codex, gemini, pi, hermes and open-claw have no verified
output-limit key (gemini's writer drops any env key outside its hardcoded
allow-list, codex's config surface has no such TOML key), so their presets
carry only the context. Changing the default is a manifest edit + re-sync,
no gateway rebuild.

The key is passed via a **`--config-file` under `umask 077`**, never on the
command line (argv is world-readable via `/proc`), and runtime values are
merged with `jq`, never `sed` string substitution:

```sh
umask 077
# compose: jq walk() over the preset, sentinel substitution
jq 'walk(...) $PRESET > "$TMP/provider.json"

# One stable id per gateway key: its last 4 chars, lowercased and
# dash-stripped to satisfy cc-switch's id charset. Re-running the script
# refreshes the same provider in place via --overwrite (our cc-switch
# fork's upsert flag), so a rotated key or changed model list replaces
# the stored config; nothing piles up.
PROVIDER_ID="adeo-litellm-$(printf '%s' "$API_KEY" | tail -c 4 \
  | tr '[:upper:]' '[:lower:]' | tr -d '-')"
cc-switch provider add --app "$HARNESS" --id "$PROVIDER_ID" \
  --name "ADEO LiteLLM" --config-file "$TMP/provider.json" --overwrite
cc-switch use "$PROVIDER_ID" --app "$HARNESS"    # the success gate
rm -f "$TMP/provider.json"
```

The `--overwrite` flag is the one fork-carried change in cc-switch
(branch `jya0-v5.11.1`): stock `provider add` is insert-only and its
only update path is the interactive edit prompt. With the flag, an
existing id is updated in place through the same ProviderService::update
the edit prompt uses; a nonexistent id fails loudly. The script also
keeps a compat path: on a stock binary the duplicate-add error for an
existing id is treated as the no-op it is (identical content), so the
script still works un-patched, minus true refresh.

`cc-switch use` writes the harness's native config file
(`~/.config/opencode/opencode.json` for open-code, `~/.claude/settings.json`
for claude — only once `~/.claude/` exists, `~/.codex/config.toml` likewise:
cc-switch writes lazily when the app's config dir is present, which is stock
behavior identical with its own field-mode add). Verified against
cc-switch-cli v5.11.1. After the loop the script runs `cc-switch env check`
to surface any conflicting environment variables.

**Revisions do not accumulate:** one provider per harness per key,
identified by the key's last 4 chars. Re-runs overwrite it in place
(the fork's `--overwrite`), so there is no stale catalog row to clean up
and no TUI housekeeping. A different key creates a different provider,
which is the intended multi-key story.

## Generator contract (Jinja2, store-backed template)

The template is served from the object store (`templates/install.sh.tmpl`)
when a copy exists, falling back to the copy baked into the gateway image
(`generator._TEMPLATE`) when the store copy is missing or unreadable. The
store copy is pushed through the admin-gated write API
(`PUT /install/admin/v1/artifact/templates/install.sh.tmpl`, `PROXY_ADMIN`
only), so iterating on the install flow is an admin sync, not an image
rebuild. See [security.md](security.md) "What lives where".

The slice's `/install` handler renders the template with **Jinja2**. The
template is a Jinja2 document, not a find-replace string, so it can grow
conditionals and loops without the renderer changing.

The render context is open: every key in `manifest.json` is exposed to the
template, plus `base_url` and the typed `platforms` / `harnesses` lists. The
keys the template actually references:

| Variable | Type | Source |
|---|---|---|
| `base_url` | str | `LITELLM_PUBLIC_BASE_URL` env/config, request host as fallback |
| `cc_switch_version` | str | `manifest.json` `cc_switch_version` |
| `max_output_tokens` | int (default 65536) | `manifest.json` `max_output_tokens` |
| `platforms` | list of `{target, archive_ext}` | `manifest.json` `platforms` |
| `harnesses` | list of `{harness, version, path, sha256}` | `manifest.json` `harnesses`; the menu list is baked from it at render time |

Rendering is sandboxed, strict, and not autoescaped
(`SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)`):

- **`StrictUndefined`** — a template that references a variable we did not
  pass raises at render time. `/install` returns 500 with the error rather
  than serving a half-rendered installer to every client.
- **`autoescape=False`** — shell metacharacters (`&`, `<`, `>`) are not turned
  into HTML entities.

The generator is pure (manifest + base URL in, script text out) and unit
tested in the slice's co-located module: the cc-switch download, the
manifest-hash verification, the harness-selection prompt, and the per-harness
backup + delete-then-add + `use` sequence all have regression tests.

A template change is a gateway code change and ships with the image (code
review + redeploy), so it is validated by the generator's unit tests before it
ever reaches a developer.

## Harness configuration lives in cc-switch

The script does not write harness config files itself and does not scan for
harnesses. It offers the list cc-switch supports, the user picks, and
cc-switch writes each chosen harness's native config. This replaces both the
retired report-only "Detected harnesses" summary and the `lite configure`
commands, which only ever covered claude and codex.

## Resume behaviour

Re-running is safe and cheap:

- The cc-switch binary install is a version-check short-circuit; a matching
  `cc-switch --version` skips the download.
- The provider reconcile is delete-then-add under a `config backup`, because
  `provider add` rejects a duplicate id and `provider edit` is interactive-only.
  cc-switch preserves unrelated keys in the harness config file across the
  `use`.
- A wrong key is caught by the `/api/v1/models` diagnostic before any harness
  file is written, so a bad key never leaves a half-configured harness.

## Idempotency and model resync

1. Script fetches `/install/manifest.json` first.
2. If `cc-switch` exists and matches the manifest's `cc_switch_version`, skip
   the download.
3. Otherwise download + manifest-hash-verify + extract in place.

**Model resync is explicit, not live.** The harness does not continuously read
`/api/v1/models`. When the gateway's model set changes, the user re-runs
`/install` (or `/install | sh -s -- --sync`): the script re-fetches
`/api/v1/models` with the key and re-writes each configured provider's model
list and per-model context windows via the same overwrite reconcile,
so one command covers both first install and resync.

Version policy: the mirror pins exactly one cc-switch version
(`cc_switch_version` in the manifest). A bump re-pins it and the admin
publishes the new archives; the outgoing set is retained until the new
manifest is live, to cover a client mid-download.

## Windows (Git Bash / WSL)

Native PowerShell `/install.ps1` is deferred. Windows is supported through the
shells that already ship bash:

- **Git Bash** (ships with Git for Windows) and **WSL** both run the script
  unchanged.
- Under Git Bash / MSYS, `uname -s` returns `MINGW64_NT-...` (or
  `MSYS_NT-...`), not `Linux`. The script maps `MINGW*` / `MSYS*` to the
  Windows cc-switch archive (`windows-x64.zip`).

## Failure modes the script must handle

- Mirror missing a platform archive -> clear message naming the exact URL that
  404'd.
- Checksum mismatch -> abort before extraction, "artifact store may be
  corrupt, contact platform team".
- Key rejected -> abort at the diagnostic, before any harness file is written.
- No `curl` -> the script cannot bootstrap; error says so.
- No `jq` -> bootstrapped from the mirror (`/install/jq/<version>/...`,
  official jqlang static binaries, SHA-256 pinned in the script because
  reading the pin from the manifest would already need jq). Only dies if the
  mirror has no build for the platform.
- No trusted CA roots (curl exit 60 or 77) -> the script names the OS package
  to install (`apt-get install ca-certificates` / `apk add ca-certificates`);
  a CA bundle cannot be fetched over TLS that itself needs the bundle.
- RustFS unreachable -> the artifact fetch returns 503 from the slice; the
  script aborts with the URL, never falls back to the internet.
