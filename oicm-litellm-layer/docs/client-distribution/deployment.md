# Deployment

## Components

1. **Artifact store.** A PVC (or OCI image; see below) holding `/mirror/*`.
2. **Artifact pod.** A tiny nginx (or caddy) Deployment serving the store as
   plain static files with correct MIME types and long cache headers.
3. **Ingress update.** Add path rules to the existing `litellm-ingress.yaml`.
4. **Sync job.** A CronJob (or manual job) that refreshes the store on a
   machine with internet, then pushes into the cluster.
5. **The `/install` scripts.** Two small shell files, served as static
   content from the artifact pod itself (they are just files; keeping them in
   the same store avoids a fourth backend).

## Ingress

Extend `deploy/prod/litellm-ingress.yaml` (current state: both hosts route
everything to `service litellm-proxy port 4000`). New rules on the same host:

```yaml
# /install and /mirror/* -> artifact service
- path: /mirror
  pathType: Prefix
  backend: {service: {name: litellm-artifacts, port: {number: 8080}}}
- path: /install
  pathType: Prefix
  backend: {service: {name: litellm-artifacts, port: {number: 8080}}}
```

Exact-path vs prefix: `/install` as `Prefix` also catches `/install.ps1` in
one rule. Path-specific rules take precedence over the existing `/` prefix to
the proxy, so `/v1` traffic is untouched. TLS: same hostname, so the existing
`litellm-ecouncil-ae-tls` secret serves it (see the ingress memory note:
secret naming is dash-based, `litellm-ecouncil-ae-tls`, and the wildcard
covers `litellm.ecouncil.ae` — one label).

Network policy: the existing `litellm-network-policy-to-adeo.yaml` pattern
extends to allow the artifact pod's egress = none (it is fully static) and
ingress = nginx ingress controller only.

## Artifact store: PVC vs OCI image

- **PVC + rsync/scp sync job** — simplest, matches the spend-logs-archival
  pattern already in this repo (Longhorn PVC + CronJob). Update = job writes
  new manifest + artifacts, atomic directory swap.
- **OCI image** — the store baked into an image tag per version; update = new
  image tag + rollout. More auditable (image digest = store digest), slower
  to update.

Recommendation: PVC + atomic directory swap, with the manifest as the
commit-point (a partially-synced store is never referenced because the
manifest is swapped last). The image approach becomes attractive if we later
want signature verification over the whole store.

## Sync job

Runs on the connected workstation (this one), not in the cluster: it needs
internet. Steps (idempotent):

1. Read target versions (upstream release to adopt: `litellm`, pinned `uv`,
   pinned Python).
2. Fetch uv release artifacts + `.sha256` sidecars for the
   [platform matrix](platform-matrix.md); verify against the published
   checksums.
3. Resolve the `litellm[cli]` wheel closure for the target Python range
   (`uv pip compile` or scratch-venv method, see
   [mirror-content.md](mirror-content.md)) and collect wheels.
4. Fetch the python-build-standalone archives for the pinned CPython.
5. Compute sha256 for everything, write `manifest.json`, swap directories,
   restart the artifact pod (or not, if PVC + static serving makes it
   unnecessary — nginx picks up files as they land).

CI supply-chain rule from the repo guidelines applies to the sync job itself:
download to file, verify checksum, never `curl | bash`; pin every tool by
full URL and version.

## Rollout sequence

1. Deploy artifact pod + ingress paths, verify with `curl` from the
   workstation (and from a locked-down VM if one is available for testing).
2. Pilot: 2-3 internal users install via `/install`, run `lite login`,
   `lite configure claude`, and use their agent for a day.
3. Fix what the pilot surfaces (MIME types, PATH shells, keyring quirks on
   headless hosts are the likely candidates).
4. Announce; retire any hand-rolled per-team setup scripts.
5. [rollout.md](rollout.md) tracks the metrics.

Sizing: uv artifacts ~20 MB x 8, Python builds ~50 MB x 6, wheelhouse
~50-150 MB, installers a few KB. Everything fits in a 5 Gi PVC with generous
headroom and a few versions retained for rollback.
