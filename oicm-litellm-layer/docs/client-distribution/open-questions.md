# Open Questions

Almost everything is settled after the v4 review. What remains is small, and
each item has a default so the plan is actionable without waiting.

Settled across v1-v4 (removed from the list):

- Where artifacts are served from -> inside the gateway as a slice.
- Storage backend -> MinIO, cluster-internal (v3's PVC was replaced in v4).
- How the store is filled -> admin REST sync API (slice talks to MinIO).
- Harness auto-selection -> dropped; detection is a reporter.
- Public path prefix -> everything under `/install`.
- Wheelhouse form -> flat `--find-links` + `--no-index`; one version only.
- Manifest signing -> not signing; master key is the trust anchor.
- Harness mirroring -> non-goal; users install their own harnesses.
- Upstream contribution -> none; proprietary `jya0-` branches only.
- CLI version policy -> always equal to the gateway version.
- Multi-replica storage -> resolved by MinIO being shared.
- `/install` authentication -> unauthenticated.
- Admin sync storage -> MinIO via the slice, master-key auth.

## 1. Windows native support timing

Git Bash / WSL is supported now (decision 4). Native `/install.ps1` is
deferred until a Windows user exists behind the firewall.

**Default:** Git Bash / WSL only; build `/install.ps1` on demand.

## 2. Admin sync API authorization scope

The admin API uses `_require_admin` (`PROXY_ADMIN`), satisfied by the master
key. If a non-master admin key should also sync artifacts (e.g. a CI key),
decide whether that stays `PROXY_ADMIN` or becomes a narrower role.

**Default:** `PROXY_ADMIN` only, matching the `oicm_routes.py` precedent.

## 3. Spend-log archival cutover to MinIO

MinIO is now deployed and the `litellm-logs` bucket is planned for the S3
logging callback (`s3_callback_params`). Whether to actually switch spend-log
archival to it (vs. the existing PVC-based janitor) is a separate, later
decision; this design only reserves the bucket and the shared client.

**Default:** reserve the bucket now; migrate archival when the existing
janitor's retention story needs revisiting.

## 4. One-previous retention window

The race guard keeps the outgoing version set briefly during a swap. How
briefly (hours vs days) is an operational choice.

**Default:** keep the previous set until the next sync, then prune.
