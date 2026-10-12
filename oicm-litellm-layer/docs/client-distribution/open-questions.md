# Open Questions

Decisions that need a human call before implementation. Each has a default so
the plan is actionable without waiting. v2 (in-gateway slice) and v3 (PVC +
admin sync API + harness reporter) resolved several, so the list shrank.

Resolved since v1/v2 and removed from this list:

- **Where artifacts are served from** -> inside the gateway as a slice.
- **Storage backend** -> PVC, not MinIO/S3 (S3 support is logging-only).
- **How the PVC gets filled** -> admin REST sync API, not `kubectl cp` or a
  CronJob.
- **Harness auto-selection** -> dropped; detection is a reporter, selection
  is the user's.

## 1. Public path prefix: `/mirror` vs something nicer

`/mirror` is collision-proof but reads as internal. `/client`, `/bootstrap`,
`/dl` are nicer but risk colliding with a future upstream route (upstream has
no such routes today — verified — but the proxy's route surface is huge).

**Default:** `/mirror`. Rename later costs one slice constant + one wiring
line.

## 2. Wheelhouse index form: flat find-links vs PEP 503

Flat `--find-links` + `--no-index` is the fail-closed choice (no resolver
freedom, impossible to reach PyPI accidentally). PEP 503 allows asking for
arbitrary versions.

**Default:** flat find-links. PEP 503 only if a real need for
multi-version selection appears.

## 3. Manifest signing

Deferring signature verification (see [security.md](security.md)). The cost
of a full store compromise is currently bounded by the internal-only posture;
the cost of key management now is real. The in-app slice makes eventual
verification cheap (public key ships in the image, generator emits the verify
step), so the deferral is cheap to reverse.

**Default:** ship unsigned-with-hashes; revisit at broad adoption.

## 4. Windows support timing

No Windows user is known behind the firewall. The design supports it
(`/install.ps1`, x64 + ARM64 artifacts in the matrix, `where`-based harness
detection) but nothing is built.

**Default:** design documented, implementation deferred until a Windows user
exists.

## 5. Should the harnesses themselves also be mirrored?

Claude Code, OpenCode, Codex each install via npm/curl/brew from their own
origins. `lite configure` wires them up but does not install them. A
hard-locked machine that cannot run `npm install -g` is stuck even with `lite`
working. Mirroring the harness installers is a scope extension of the same
pattern.

**Default:** out of scope for v1; track as the most likely v2 feature. The
pilot will reveal whether locked-down machines in practice already have these
tools.

## 6. Upstream contribution

The `/install` template is a fork of upstream's `scripts/install-cli.sh` with
origin overrides. Upstream might take a PR making the origins configurable
(env vars already cover most of it). Separately, the gateway-side slice (a
`/install`-serving pattern for locked-down deployments) could be proposed
upstream once the pilot proves it.

**Default:** keep the fork small and local; propose upstream after the pilot.

## 7. Adoption cadence for `litellm` releases

The mirror pins one `litellm[cli]` version at a time. Adopting a new upstream
release = admin sync re-run + manifest publish. How far behind upstream we are
willing to run is a policy call.

**Default:** adopt at most one minor behind latest stable, review CLI-related
upstream changelogs before publishing the manifest.

## 8. Multi-replica gateways and the artifact PVC

The gateway is single-replica today. If it ever scales out, the artifact PVC
needs `ReadWriteMany` (Longhorn RWX), or the artifacts bake into the gateway
image. This now matters slightly more than before because the volume is
writable (admin sync API), so two replicas would both be able to write.
Not a v1 problem; must be remembered before any horizontal scaling.

**Default:** `ReadWriteOnce` PVC now; a pre-scaling checklist item lives in
[deployment.md](deployment.md).

## 9. Authenticated / personalized `/install` (v2-enabled future)

The in-gateway slice can take `Depends(user_api_key_auth)` and emit a script
keyed to the caller (their team's default model, their entitlements). v1
stays unauthenticated because of the bootstrap paradox. If adopted later, the
key must be passed as a header on the `/install` request (not embedded in a
piped URL).

**Default:** unauthenticated generic script; revisit only if a per-team
default-model need appears.

## 10. Admin sync API write-authorization scope

The admin API uses `_require_admin` (`PROXY_ADMIN`), which the master key
satisfies. If a non-master admin key should also be able to sync artifacts
(e.g. a dedicated CI key), decide whether that is `PROXY_ADMIN` only or a
narrower role. Not urgent: the master key is the intended caller today.

**Default:** `PROXY_ADMIN` only, matching the `oicm_routes.py` precedent.
