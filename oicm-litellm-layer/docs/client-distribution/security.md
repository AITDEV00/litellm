# Security

## Threat model

Assets: developer machines (the script runs as the user, writes `~/.local/bin`
and `~/.litellm`), and the artifact store's integrity (what a compromised
store could push to developers).

Trust anchors: TLS to `litellm.ecouncil.ae` (the firewall already requires
this hostname; the cert serving is the existing DigiCert `*.ecouncil.ae`
secret — verified in the ingress memory notes), and the manifest's hashes.

## Why the artifact routes are unauthenticated static files

- The scripts and wheels contain no secrets: no API keys, no credentials. The
  key enters the picture only inside the stock CLI (`lite login` / `--api-key`
  / env), which talks to the authenticated `/v1` surface.
- Adding a key gate on `/install` would create a bootstrap paradox (you need
  the CLI to get the token, but the token to get the CLI).
- Personalization-by-key was considered and rejected in the earlier research:
  a plain `curl` of a static path carries no caller identity, and asking the
  user to pass a key to a piped shell would put secrets into shell history
  and process lists.

What unauthenticated does allow: anyone inside the network can download the
CLI. That is the same posture as every internal tool's docs page, and the CLI
without a key is useless. Rate-limiting at nginx is a cheap add if abuse ever
shows up.

## Integrity chain

1. Every artifact download is checksum-verified by the install script against
   `manifest.json` (fetched first, over the same TLS).
2. The uv installer verifies nothing itself when re-pointed via
   `INSTALLER_DOWNLOAD_URL`, so our script does the sha256 check before
   piping the installer — the installer script is treated as an artifact like
   any other.
3. The wheelhouse is installed with `--no-index`, so a compromised or stale
   mirror cannot cause resolution to fall back to PyPI; a tampered wheel
   either fails the hash or fails to install.
4. `lite config set base_url` normalization (verified in source) rejects
   credentials/fragments in the URL, so the persisted gateway URL cannot
   smuggle a hostile credential.

Residual risk: the manifest itself. A store compromise can rewrite the
manifest too, so hash-checking against it is tamper-evidence against
accidental corruption, not against a full store compromise. Full end-to-end
assurance would need signature verification (below).

## Signature verification (deferred, decision recorded)

The clean form: the sync job signs the manifest with a private key kept on
the sync machine; the install script embeds the public key and verifies the
manifest signature before trusting any hash. That makes a static-store
compromise insufficient to push malicious tooling, at the cost of key
management.

Deferred because: the firewall already restricts reachability to the
hostname, TLS protects transport, and the initial user population is
internal. Revisit when [rollout.md](rollout.md) metrics show broad adoption
or if the store moves to an externally-reachable surface. Logged in
[open-questions.md](open-questions.md).

## Notes on the piped-curl pattern

`curl | sh` is normally an anti-pattern (you run bytes you cannot inspect).
Here it is acceptable because: the script is ours, versioned in this repo,
served over the same TLS as the API itself, and hash-pinned via the manifest.
Still, the docs will offer the two-step form for cautious users:

```sh
curl -fsSL https://litellm.ecouncil.ae/install -o lite-install.sh
less lite-install.sh
sh lite-install.sh
```

## Supply-chain hygiene for the sync job

From the repo's CI supply-chain rules, applied to the mirror builder:
download to file + verify SHA-256 before use; pin every external tool by exact
version and full URL; prefer official `.sha256` sidecars (Astral publishes
them) over third-party checksum pages.
