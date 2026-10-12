# Implementation Plan

Status: not started. Step-by-step build order for the v4 design
([PLAN.md](PLAN.md), [architecture.md](architecture.md),
[minio.md](minio.md), [admin-sync.md](admin-sync.md)).

Everything below is grounded in the **current** source tree and the **live**
`adeo-litellm` namespace as of 2026-10-12:

- Gateway image: `registry.adeoaiengine.ecouncil.ae/openinnovationai/platform/mlops/mlops-serving/litellm-src:jya0-v1.102.0`
- `deployment/litellm-proxy` runs **2 replicas** (this matters: the MinIO
  backend is what makes multi-replica safe; a `ReadWriteOnce` PVC would have
  blocked this)
- Ingress `litellm-proxy` serves both hosts, `Prefix /` -> `litellm-proxy:4000`,
  TLS via `litellm-ecouncil-ae-tls` (dash-named secret)
- **No MinIO deployed yet** in `adeo-litellm`; no `NetworkPolicy` objects exist
  in the namespace at all
- Secrets in-cluster: `litellm-master-key` (prod) and
  `litellm-master-key-dev` (dev mounts this one, verified live) — the admin
  sync key therefore differs per environment
- Pod labels (verified live): prod `app=litellm-proxy`, dev
  `app=litellm-proxy-dev` — usable as NetworkPolicy selectors
- Slice pattern precedent: `litellm/proxy/oicm_routes.py`, wired at
  `proxy_server.py:18897-18899` in a fork-appended block, pinned by
  `tests/test_litellm/proxy/test_oicm_drop_detection.py`

## The operating rule: everything lands on dev first

**Nothing touches prod until dev is fully verified.** Concretely:

1. Every phase below runs against the **dev** overlay
   (`make litellm-src-release-dev`, `kubectl apply -k deploy/overlays/dev`)
   and against the dev Deployment (`litellm-proxy-dev`) only.
2. Dev is proven by living with it: run the whole flow (install, login,
   configure, agent turns) on dev, and let it soak until there is confidence
   in the configuration.
3. **No shadow commands.** Nothing is fixed or created by hand in the
   cluster (`kubectl exec`, `mc mb` ad hoc, hot patches, imperative edits).
   Every change lands as an idempotent manifest in git, so the deployed
   state is always reproducible from `kubectl apply`. The one exception in
   this plan is one-shot bucket creation, which is itself a manifest
   (a Job with `mc mb --ignore-existing`, so it is idempotent and re-runnable
   from git, not a hand command).
4. Once dev is verified and the manifests are known-good, prod gets **one
   round of clean `kubectl apply -k deploy/overlays/prod`** (plus the
   ImagePull restart the Makefile performs). No incremental prod changes, no
   prod-specific hand edits: prod receives exactly the bytes dev validated.

Why this is safe here: dev and prod share the same image tag
(`litellm-src:$(LITELLM_SRC_TAG)`, per the Makefile caveat), so the dev
build already carries the prod payload; the prod rollout only pins the
manifests and restarts. The slice itself is additive (one router include),
so the prod apply is one atomic, revertible change.

Phases are ordered so each one is independently verifiable and the dev
gateway never serves a broken `/install` at any point. Phases 1-5 are
dev-only. Phase 6 is the single prod cut-over.

---

## Phase 1 — MinIO deployment (no code, no risk, DEV)

**Goal:** a running MinIO with both buckets, reachable only from the
gateways. Deployed to the namespace now, but nothing consumes it until
Phase 3 wires the slice; the dev gateway gets env vars first, prod's are
added only at Phase 6.

New manifest `deploy/base/shared/minio.yaml` (follows the
`litellm-redis-password.yaml` precedent of living in shared):

```yaml
apiVersion: v1
kind: Secret
metadata: {name: litellm-minio-credentials, namespace: adeo-litellm}
type: Opaque
stringData: {access-key: ..., secret-key: ...}   # generate, never commit real values
---
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: minio, namespace: adeo-litellm}
spec:
  serviceName: minio
  replicas: 1
  template:
    metadata: {labels: {app: minio}}
    spec:
      containers:
        - name: minio
          image: docker.io/minio/minio:RELEASE.2026-04-01T00-00-00Z   # pin exactly; verify sha256 from the official release notes
          args: ["server", "/data", "--console-address", ":9001"]
          env:
            - {name: MINIO_ROOT_USER, valueFrom: {secretKeyRef: {name: litellm-minio-credentials, key: access-key}}}
            - {name: MINIO_ROOT_PASSWORD, valueFrom: {secretKeyRef: {name: litellm-minio-credentials, key: secret-key}}}
          ports: [{containerPort: 9000}, {containerPort: 9001}]
          volumeMounts: [{name: data, mountPath: /data}]
          readinessProbe: {httpGet: {path: /minio/health/ready, port: 9000}}
  volumeClaimTemplates:
    - metadata: {name: data}
      spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 10Gi}}}
---
apiVersion: v1
kind: Service
metadata: {name: minio, namespace: adeo-litellm}
spec: {selector: {app: minio}, ports: [{port: 9000, targetPort: 9000}]}
```

Plus, in the same manifest, two things that keep everything idempotent:

- A **one-shot bootstrap Job** (part of the manifest, not a hand command):
  `mc mb --ignore-existing litellm-client-mirror` and
  `mc mb --ignore-existing litellm-logs`, using the same Secret. Re-applying
  the manifest is safe; the Job is `restartPolicy: Never` with
  `backoffLimit` and `--ignore-existing` making bucket creation idempotent.
  (Alternatively set `MINIO_VOLUMES`-style bootstrap via MinIO's own
  `mc` init container; the Job is simpler and visible in `kubectl get jobs`.)
- A **NetworkPolicy** (the first in this namespace) allowing ingress to
  `app: minio` port 9000 only from pods with label `app: litellm-proxy` OR
  `app: litellm-proxy-dev` (both verified live), plus the bootstrap Job's
  label. This manifest is shared by dev and prod by design: MinIO is a
  common dependency, and the policy covering both labels is what makes the
  Phase 6 prod apply a no-surprise diff.

Steps (all dev-scoped verification):

1. Generate credentials; commit the Secret manifest following the same
   convention as `litellm-master-key.yaml` in this private repo.
2. `kubectl apply -k deploy/overlays/dev` (the overlay includes
   `base/shared`), then verify:
   `kubectl -n adeo-litellm rollout status statefulset/minio`, Job completes,
   buckets listed by the Job's log.
3. Verify isolation: from a pod in another namespace, the S3 endpoint must
   NOT be reachable; from a `litellm-proxy-dev` pod, it must.

**Exit:** MinIO ready, both buckets exist (created by the Job, not by hand),
gateway-only reachability confirmed. Prod is untouched: the shared manifest
is in git, but `deploy/overlays/prod` has not been applied and no env var
points prod at MinIO yet.

---

## Phase 2 — the slice (code, unit-tested, not yet wired)

**Goal:** `litellm/proxy/client_distribution/` exists, fully tested, not yet
mounted. Environment-neutral: code and tests only, nothing applied anywhere.

Files, following the `oicm_routes.py` structure exactly:

```
litellm/proxy/client_distribution/
├── __init__.py        ← re-exports router
├── routes.py          ← APIRouter + all handlers (public reads, generated /install, admin writes)
├── generator.py       ← pure function: manifest + base_url -> script text
└── storage.py         ← boto3/botocore client factory reading the shared env vars
```

Key implementation points, each grounded in what exists:

- **storage.py** reads `MINIO_ENDPOINT_URL`, `MINIO_ACCESS_KEY`,
  `MINIO_SECRET_KEY` (new env vars added in Phase 3). boto3 is already a
  litellm main dependency (`pyproject.toml`: `boto3>=1.43.1,<2.0`), so no new
  dependency. Use the `BaseAWSLLM`-free simple path: a plain
  `boto3.client("s3", endpoint_url=...)` created once per worker, matching how
  `litellm/integrations/s3.py` builds its client from params.
- **routes.py**:
  - `GET /install` — unauthenticated; calls `generator.render(...)`; returns
    `text/x-shellscript` with `Content-Type: application/x-sh`. Source of
    `base_url`: `LITELLM_PUBLIC_BASE_URL` env (add in Phase 3) with
    request-host fallback.
  - `GET /install/{path:path}` — unauthenticated proxy read from MinIO
    (`get_object`, stream). 404 if absent, 503 if MinIO down. Content types
    by extension (`.json`, `.tar.gz`, `.zip`, `.whl`, `.sha256`).
  - `* /install/admin/v1/*` — `dependencies=[Depends(user_api_key_auth)]` +
    `_require_admin` copied from the `oicm_routes.py` precedent
    (`LitellmUserRoles.PROXY_ADMIN` check). Handlers: manifest GET/PUT,
    artifact PUT (stream to MinIO, sha256 on the fly, skip-if-same), artifact
    DELETE (409 if manifest-referenced), inventory GET.
  - Path containment: normalize the key against `litellm-client-mirror/`
    and reject escapes (cannot touch `litellm-logs`).
- **generator.py**: pure, no FastAPI imports. Template is upstream's
  `scripts/install-cli.sh` with the three origin modifications from
  [installer-script.md](installer-script.md). Two-phase A/B split and
  `~/.litellm/install_state.json` progress file per the resume design.
  Harness detection (report-only) per the v3/v4 sections.
- Follow repo code conventions: `Final` annotations, frozen dataclasses,
  `MappingProxyType`, typed everything, no `Any`. `# noqa` only with named
  rule + reason.

Tests (co-located, mirroring `tests/test_litellm/proxy/client_distribution/`):

- `test_generator.py` — render with a known manifest + base URL; assert the
  three origin overrides, the `lite config set` line, phase markers, and
  that no upstream URL (`astral.sh`, `github.com`, `pypi.org`) appears in
  the output.
- `test_routes.py` — handler-level tests with a stubbed storage layer
  (dependency-injected, not monkeypatched, per repo rules): unauth GET works;
  admin routes 403 for non-admin; manifest PUT rejects a manifest referencing
  a missing object (409); artifact PUT is idempotent (second identical PUT ->
  `unchanged: true`); DELETE rejects while manifest-referenced; path escape
  rejected.
- Drop-detection additions to
  `tests/test_litellm/proxy/test_oicm_drop_detection.py`:
  `test_client_distribution_router_mounted_on_app` (route paths present),
  `test_client_distribution_module_has_router` (router non-empty). Pattern
  copied verbatim from `test_voice_routes_router_is_mounted_on_app`.

**Exit:** all tests green locally
(`python -m pytest tests/test_litellm/proxy/client_distribution/ -q` and the
two new drop tests).

---

## Phase 3 — wiring (one block, both env vars, DEV FIRST)

**Goal:** the slice mounts on the app; the dev gateway can reach MinIO.

1. `proxy_server.py`: add to the fork-appended block (after line 18899, next to
   the oicm_status_router include):

```python
# OICM same-origin client distribution (/install). Co-located vertical slice.
from litellm.proxy.client_distribution.routes import router as client_distribution_router

app.include_router(client_distribution_router)
```

2. Env vars, split by the parity-test rule above:

```yaml
# BASE deployment (deploy/base/gateway/proxy/deployment.yaml) — identical
# for dev and prod, so the parity test stays quiet:
- name: MINIO_ENDPOINT_URL
  value: "http://minio.adeo-litellm.svc.cluster.local:9000"
- name: MINIO_ACCESS_KEY
  valueFrom: {secretKeyRef: {name: litellm-minio-credentials, key: access-key}}
- name: MINIO_SECRET_KEY
  valueFrom: {secretKeyRef: {name: litellm-minio-credentials, key: secret-key}}

# DEV overlay (deploy/overlays/dev/gateway/deployment.yaml) — value differs
# per environment, declared in _EXPECTED_ENV_DIFFS:
- name: LITELLM_PUBLIC_BASE_URL
  value: "https://<dev-host>"   # confirm the dev host before apply
```

   The **base** deployment gets the same block only at Phase 6, so prod never
   receives a half-verified env change. (MinIO creds are shared dev/prod by
   design — same store, see [minio.md](minio.md) — but the base-URL env
   differs per environment, which is exactly what the overlay is for.)

   The **parity test is the tripwire for this split.**
   `oicm-litellm-layer/tests/deploy/test_dev_prod_parity.py` fails if dev and
   prod drift in any way the overlay does not declare: env vars present in
   only one environment must be added to `_DEV_ONLY_ENV_VARS` /
   `_PROD_ONLY_ENV_VARS` with a reason, and value differences to
   `_EXPECTED_ENV_DIFFS`. It also fails on stale declarations, so every
   move between phases must update the test in the same commit.

   The clean split, given the test's shape:

   - `MINIO_ENDPOINT_URL` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` go in
     the **base** deployment in this phase. They are identical for both
     environments (one shared store), so the parity test stays quiet: env
     keys match, values match. Prod mounting MinIO creds before its router
     is wired is harmless — env presence is not behavior, and the Phase 6
     diff for prod shrinks accordingly.
   - `LITELLM_PUBLIC_BASE_URL` stays **overlay-scoped** with its per-host
     value (dev: the dev host; prod at Phase 6: `https://litellm.ecouncil.ae`),
     declared in `_EXPECTED_ENV_DIFFS`. The values genuinely differ, so
     overlay-scoped is the honest permanent shape; do not move it to base.
   - Never add anything to `_DEV_ONLY_ENV_VARS` for this feature — a var
     that only ever exists on dev is exactly the half-shipped state the
     parity test exists to catch.

3. Release to dev: `make litellm-src-release-dev` (builds
   `litellm-src:$(LITELLM_SRC_TAG)`, pushes to Harbor, pins the base image,
   and rolls out **only the dev Deployment**; prod keeps serving its
   already-running containers — that is the documented behavior of the
   target). CAUTION from the Makefile: dev and prod share the same image tag,
   so this build overwrites the bytes behind the tag for both; the dev-only
   rollout is what keeps prod untouched.

**Verify (dev only):**

```sh
curl -fsSL <dev-host>/install                      # generated script
curl -fsSL <dev-host>/install/manifest.json       # 404 until Phase 4, but route resolves
kubectl -n adeo-litellm logs deploy/litellm-proxy-dev | grep -i "client_distribution" | head
```

The dev host is reachable via port-forward per the datasource runbook
(`kubectl -n adeo-litellm port-forward deploy/litellm-proxy-dev 14001:4000`)
or via its ingress host if one is wired; confirm which before running the
curls.

**Exit:** `/install` serves the generated script on **dev**; empty store
404s on artifact reads, not on the script itself. Prod untouched.

---

## Phase 4 — artifact sync (populate the store, shared but keyed to dev first)

**Goal:** the buckets contain the mirror content.

The store is shared (one MinIO, both gateways can read it), but the sync
writes are made through the **dev gateway's** admin API so the write path
itself is what gets exercised and verified.

The fetch script lives in `examples/` per layer conventions (runnable things
are not docs): `oicm-litellm-layer/examples/client-distribution/sync-mirror.sh`.

Run on this workstation (internet access):

1. Fetch uv 0.10.9 release artifacts + `.sha256` sidecars for the 8
   [platform-matrix.md](platform-matrix.md) targets from the astral-sh/uv
   GitHub release; verify each checksum.
2. Resolve the `litellm[cli]` closure at the gateway's version
   (`litellm_cli_version` == gateway version per decision 7):
   `uv tool install 'litellm[cli]==1.102.0'` into a scratch venv, copy the
   wheels out of uv's cache (`uv cache dir`), or `uv pip compile` equivalent.
3. Fetch the python-build-standalone archives for the pinned CPython
   (`3.13.9`) matching the pinned uv's expectations.
4. Fetch the uv installer script itself
   (`https://astral.sh/uv/0.10.9/install.sh`), checksum it.
5. Compute sha256 for everything; write `manifest.json` per the schema in
   [mirror-content.md](mirror-content.md).
6. Push per the [admin-sync.md](admin-sync.md) loop **against the dev
   gateway**: artifacts first
   (`PUT /install/admin/v1/artifact/...`), manifest last (`PUT
   /install/admin/v1/manifest`). The bearer token is the **dev** master key
   (the `litellm-master-key-dev` Secret, verified live as the one
   `litellm-proxy-dev` mounts): read it at run time from the Secret, never
   hardcode it.

Supply-chain rules apply to the fetch script: download to file + verify
SHA-256 before use; pin every tool by exact version + full URL; never
`curl | bash`.

**Exit:** `curl -fsSL <dev-host>/install/manifest.json` returns the real
manifest; `inventory` (with the dev key) lists all objects with matching
hashes. Prod still serves nothing from `/install` because it has no env vars
and no router wiring yet — by design.

---

## Phase 5 — end-to-end validation (DEV)

**Goal:** a clean machine installs `lite` with zero external egress, entirely
against dev.

Per [rollout.md](rollout.md) Phase 0, hardened for this environment:

```sh
# On this workstation (which HAS internet, so use network isolation):
docker run -it --network none \
  -v /tmp/mirror-seed:/seed:ro \
  ubuntu:24.04 bash
# inside: curl against a localhost:8000 seed copy first (Phase 0 of rollout.md)
```

Full e2e against the **dev** gateway (workstation with
`HTTPS_PROXY=http://127.0.0.1:1` to fail-closed any accidental egress, or the
port-forward):

```sh
curl -fsSL <dev-host>/install | sh
lite --version
lite login                       # or: --api-key from the env
lite configure claude            # or: lite opencode / lite codex
claude                           # a real turn through the dev gateway
```

Soak: per the operating rule, live with this on dev — repeat installs,
re-runs (the resume/idempotency path), an update cycle (re-sync a manifest
bump), wrong-key recovery — until the configuration is boring. Fix anything
that surfaces by editing the manifests/code in git and re-applying to dev,
never by hand-patching the cluster.

**Exit:** `lite` installs, authenticates, configures the harness, and a real
agent turn routes through `/v1` on dev with the user's key; `--no-index`
resolution shows zero external requests; a re-run of `/install` is a no-op.

---

## Phase 6 — the one clean prod apply, then pilot and GA

**Goal:** prod receives, in one round, exactly what dev validated.

Pre-flight (all read-only):

```sh
kubectl kustomize deploy/overlays/prod | kubectl diff -k deploy/overlays/prod   # review: ONLY the expected delta
kubectl -n adeo-litellm rollout status deployment/litellm-proxy                  # prod healthy before
```

The single prod round (this is the entire prod change):

1. Prod overlay gets its `LITELLM_PUBLIC_BASE_URL` value
   (`https://litellm.ecouncil.ae`), declared in `_EXPECTED_ENV_DIFFS` — or the
   existing dev declaration simply keeps covering it, since the test already
   expects that one value difference. The `MINIO_*` vars are already in the
   base (Phase 3), identical for both environments. The router include is
   already in the image bytes (same tag, from the dev build). So the actual
   prod delta at this point is: the prod `LITELLM_PUBLIC_BASE_URL` value (if
   not yet present), the image pin (if the tag moved), and nothing else.
2. `kubectl apply -k deploy/overlays/prod` — one clean apply of the
   known-good manifests. This picks up: MinIO (already running, no-op
   diff), the NetworkPolicy (no-op, already applied with dev), the gateway
   env vars, and the image pin. Then
   `kubectl -n adeo-litellm rollout restart deployment/litellm-proxy` +
   `rollout status` (or `make litellm-src-release` once, which does build +
   pin + apply + restart in one step — but since the image is already
   pushed from the dev phase, the apply-and-restart alone is the cleanest
   single prod action).
3. Verify prod: `curl -fsSL https://litellm.ecouncil.ae/install`,
   `/install/manifest.json`, and one full user journey (install -> login ->
   configure -> one agent turn).

If anything looks wrong in the `kubectl diff` pre-flight, stop: fix it in
git, re-run the dev phases affected, and come back. Prod never gets an
unverified delta.

Then pilot and GA per [rollout.md](rollout.md): 2-3 internal users, one
week, watch install success rate / keyring / PATH quirks / `/v1` latency
during parallel installs. Then announce with the two-command story and the
cautious two-step form from [security.md](security.md).

---

## Definition of done

- Phases 1-5 all green **on dev**, and dev has soaked (install, re-run,
  wrong-key recovery, update cycle)
- The Phase 6 pre-flight diff showed only the expected delta; the single
  prod apply succeeded; prod serves the full flow
- Drop-detection tests green in CI (`test_oicm_drop_detection.py`)
- `make check` passes on the touched files (the repo-wide gate)
- The mkdocs build renders this folder clean
- A real user (you) has installed `lite` via `https://litellm.ecouncil.ae/install`
  and completed a real Claude Code / OpenCode turn through the prod gateway

## Rollback at every phase

- Phase 1: `kubectl delete -f` the MinIO manifests (or git-revert + apply);
  nothing consumes them yet. The StatefulSet PVC can be retained or deleted
  with it.
- Phase 2: pure code addition; revert the commit
- Phase 3: revert the two-line wiring + env vars; the drop tests trip by
  design (that is the tripwire working). Re-apply the dev overlay.
- Phase 4: the store being empty only 404s artifact reads; `/install` script
  itself still serves (it does not read the store to render). Delete objects
  via the admin API if a clean slate is wanted.
- Phase 5: nothing to roll back on dev beyond Phase 3/4 reverts.
- Phase 6: revert the base/overlay manifest edits and re-apply
  `-k deploy/overlays/prod` + rollout restart — prod returns to its
  pre-change state in one round, the same way it received the change.
  Installed clients keep working; `lite` talks only to `/v1`.
