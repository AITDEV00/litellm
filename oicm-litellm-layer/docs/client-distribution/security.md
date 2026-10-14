# Security

## Threat model

Assets: developer machines (the script runs as the user, writes `~/.local/bin`
and harness config dirs like `~/.config/opencode` and `~/.cc-switch`), and the
artifact store's integrity (what a compromised store could push to
developers).

Trust anchors: TLS to `litellm.ecouncil.ae` (the firewall already requires
this hostname; the cert serving is the existing DigiCert `*.ecouncil.ae`
secret), RustFS's cluster-internal isolation, and the manifest's hashes.

## What lives where (the trust split)

The single most important security property of this design: **executable
bootstrap logic and mutable data are in different trust domains.**

```
Gateway image / git (code review required to change):
  baked-in install template   <- the fallback script
  installer rendering logic

RustFS (admin-channel write authority required to change):
  templates/install.sh.tmpl       <- the live template, admin-synced
  cc-switch/<version>/<archives>  <- immutable, versioned
  manifest.json                   <- versioned, ETag/compare-and-swap
```

The install template is **served from RustFS when a store copy exists**, and
the baked-in copy is the fallback when the store copy is missing or
unreadable. This is a deliberate choice for iteration speed: tuning the
install flow should not need a gateway image rebuild each time. The guardrail
is that the store write path (`PUT /install/admin/v1/artifact/templates/
install.sh.tmpl`) is gated by the same `PROXY_ADMIN` check as every other
artifact write — only an admin key can change the template, the same authority
that can publish artifacts and the manifest. A non-admin bucket credential
alone is not enough to reach it through the API.

The residual risk is accepted knowingly: an admin key holder can change the
shell developers execute. That is already true of the artifacts + manifest the
same key publishes, so the template does not widen the admin's existing
authority. If template writes are ever delegated to a narrower role, the
template should move back into the image at that point.

## Why `/install` and the artifact reads are unauthenticated (decision 9)

- The script and the artifacts contain no secrets: no API keys, no
  credentials. Downloading the binary needs no identity.
- The user supplies their gateway virtual key when the script prompts for it
  (hidden input), which is where it belongs; the install path never logs or
  echoes it.
- Personalization-by-key was considered and rejected: a plain `curl` of a
  static path carries no caller identity, and asking a user to pass a key to a
  piped shell would put secrets into shell history and process lists. The key
  is passed to cc-switch only via a `--config-file` written under `umask 077`,
  never as a command-line argument (argv is world-readable via `/proc`).

What unauthenticated does allow: anyone inside the network can download the
CLI. Same posture as every internal tool's docs page, and the CLI without a key
is useless. Rate-limiting is a cheap add if abuse ever shows up.

RustFS itself is not exposed: no ingress, and a `NetworkPolicy` allows only the
gateway. The dev machine never talks to RustFS; it only ever reaches the
gateway, which proxies the bytes.

## Write surface

The admin sync API (`/install/admin/v1/*`, see [admin-sync.md](admin-sync.md))
is the one authenticated surface. Its security properties:

- **Admin-only.** `Depends(user_api_key_auth)` plus `_require_admin`
  (`PROXY_ADMIN`), the same pattern as `litellm/proxy/oicm_routes.py`. The
  master key satisfies it; a non-admin virtual key gets `403`.
- **Prefix containment.** Every artifact key is normalized against the
  `litellm-client-mirror` bucket prefix; `..` and escapes are rejected, so the
  write API cannot reach the `litellm-logs` bucket or any other key.
- **Body size cap.** A per-request limit (reject `413` above it) so a runaway
  client cannot fill the bucket in one request.
- **Manifest integrity.** The manifest `PUT` verifies every referenced object
  exists with a matching hash and rejects `409` otherwise, so a published
  manifest never lies about the store.
- **Separate prefix.** Write (`/install/admin/*`) and read (the rest of
  `/install/*`) are distinct sub-paths, so the ingress can restrict the write
  surface to an admin network range without affecting public reads.

The master key never appears in a tracked file: it lives in the
`litellm-master-key` Secret and is supplied to the admin sync script from the
environment, never committed. RustFS credentials likewise live only in the
`litellm-rustfs-credentials` Secret, referenced by `secretKeyRef`.

## Integrity chain

1. The install script fetches `/install/manifest.json` first, over the same
   TLS. **The manifest's SHA-256 values are the client-side source of truth.**
2. At ingest time the admin sync downloads each archive and verifies it
   against upstream's `checksums.txt`; only the verified hash goes into the
   manifest. The client never sees upstream's checksums file.
3. The script verifies each downloaded archive against the manifest hash
   before extracting.
4. The key the script writes into harness configs is the user's own gateway
   virtual key, validated against `/v1/models` before any file is touched.

Residual risk: the manifest itself. A store compromise can rewrite the
manifest and the binaries together, so hash-checking is tamper-evidence
against accidental corruption, not against a full store compromise. The
**bootstrap script is out of the store's reach** (baked in the image), which
is the property that bounds the blast radius: a store attacker can at worst
deny service or serve a hash-mismatched binary (both detected), not inject
shell.

## No manifest signing (decision 3)

Signing was dropped deliberately. The reasoning: the sole writer is a
master-key-authenticated admin, so an attacker who can rewrite the manifest
already holds the master key and has bigger problems. The sha256 hashes stay,
because they still do work — corruption detection and the idempotency check on
the write path. Dropping the signature drops the signature, not the hashes.

If the store ever becomes externally reachable, revisit; the in-app slice
makes adding verification cheap (public key rides the image, the generator
emits the verify step). Ed25519 signing of `manifest.json` at admin-sync time
is the natural upgrade if that day comes.

## Client-side credential state

credentials land in two places, both user-owned and permission-checked:

- **cc-switch state** at `~/.cc-switch/cc-switch.db`. cc-switch v5.11.1 writes
  `~/.cc-switch` as `0700` and the db as `0600` (verified; the earlier
  permissive-mode report, cc-switch-cli issue #212, is closed).
- **harness-native configs** it writes (`~/.config/opencode/opencode.json`,
  `~/.claude/settings.json`, `~/.codex/config.toml`, ...). E2E asserts these
  are `0600`.

The master/admin key never enters any of this: it authorizes only the
`/install/admin/*` write surface and is never typed into the installer.

## cc-switch self-update is out of band

cc-switch has its own `update` command that resolves the public GitHub
releases — unreachable by design in this environment, and a bypass of our
mirror if it were reachable. Policy: **`/install` is the authoritative
update mechanism**; users do not run `cc-switch update`. There is no
`--source` override on the command (verified v5.11.1), so this is enforced by
documentation + the firewall, not config.

## Notes on the piped-curl pattern

`curl | sh` is normally an anti-pattern. Here it is acceptable because the
script is ours, baked into a versioned gateway image, served over the same TLS
as the API, and it hash-pins the binary via the manifest. The docs still offer
the two-step form for cautious users:

```sh
curl -fsSL https://litellm.ecouncil.ae/install -o install.sh
less install.sh
sh install.sh
```

## Supply-chain hygiene for the admin sync script

From the repo's CI supply-chain rules, applied to the fetch step: download to
file + verify SHA-256 before use; pin every external tool by exact version and
full URL; prefer official `.sha256` sidecars (the cc-switch release publishes
`checksums.txt`) over third-party checksum pages.
