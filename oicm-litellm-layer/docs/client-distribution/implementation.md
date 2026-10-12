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
- Secrets in-cluster: `litellm-master-key`, `litellm-redis-password`, etc.
  (deployed from `deploy/base/gateway/secrets/*.yaml` via kustomize)
- Slice pattern precedent: `litellm/proxy/oicm_routes.py`, wired at
  `proxy_server.py:18897-18899` in a fork-appended block, pinned by
  `tests/test_litellm/proxy/test_oicm_drop_detection.py`

Phases are ordered so each one is independently verifiable and the gateway
never serves a broken `/install` at any point.

---

## Phase 1 — MinIO deployment (no code, no risk)

**Goal:** a running MinIO with both buckets, reachable only from the gateway.

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

Plus a `NetworkPolicy` (first one in this namespace) allowing ingress only
from pods labeled `app: litellm-proxy` on port 9000. Note the gateway pods
need that label added (check the live template; if absent, add via the base
deployment).

Steps:

1. Generate credentials; create the Secret (`kubectl apply -f` the manifest
   with real values, or `kubectl create secret` and keep the manifest with
   placeholders in git — match how `litellm-master-key.yaml` is handled: it
   ships with real values in the private repo, which is the established
   pattern here for non-cluster-credential secrets).
2. Apply and verify: `kubectl -n adeo-litellm rollout status statefulset/minio`,
   then from the gateway pod (or a debug pod with the minio client): create
   the two buckets `litellm-client-mirror` and `litellm-logs`
   (`mc mb --ignore-existing`, idempotent; can be a one-shot Job).
3. Verify isolation: from a pod outside the namespace, the S3 endpoint must
   NOT be reachable.

**Exit:** MinIO ready, buckets exist, gateway-only reachability confirmed.

---

## Phase 2 — the slice (code, unit-tested, not yet wired)

**Goal:** `litellm/proxy/client_distribution/` exists, fully tested, not yet
mounted.

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

## Phase 3 — wiring (one block, both env vars)

**Goal:** the slice mounts on the app; the gateway can reach MinIO.

1. `proxy_server.py`: add to the fork-appended block (after line 18899, next to
   the oicm_status_router include):

```python
# OICM same-origin client distribution (/install). Co-located vertical slice.
from litellm.proxy.client_distribution.routes import router as client_distribution_router

app.include_router(client_distribution_router)
```

2. `deploy/base/gateway/proxy/deployment.yaml`: add the env vars (alongside
   `LITELLM_MASTER_KEY` etc.):

```yaml
- name: MINIO_ENDPOINT_URL
  value: "http://minio.adeo-litellm.svc.cluster.local:9000"
- name: MINIO_ACCESS_KEY
  valueFrom: {secretKeyRef: {name: litellm-minio-credentials, key: access-key}}
- name: MINIO_SECRET_KEY
  valueFrom: {secretKeyRef: {name: litellm-minio-credentials, key: secret-key}}
- name: LITELLM_PUBLIC_BASE_URL
  value: "https://litellm.ecouncil.ae"
```

3. Release: `make litellm-src-release` on the branch (builds
   `litellm-src:jya0-v1.102.0` or the current branch tag, pushes to Harbor,
   pins the manifest, applies overlays, restarts). CAUTION from the Makefile:
   dev and prod share the same image tag; a build overwrites the bytes for
   both. Ship to dev first (`litellm-src-release-dev`), verify, then prod.

**Verify:**

```sh
curl -fsSL https://litellm.ecouncil.ae/install          # generated script
curl -fsSL https://litellm.ecouncil.ae/install/manifest.json   # 404 until Phase 4 syncs, but route resolves
kubectl -n adeo-litellm logs deploy/litellm-proxy | grep -i "client_distribution" | head
```

**Exit:** `/install` serves the generated script on prod; empty store 404s on
artifact reads, not on the script itself.

---

## Phase 4 — artifact sync (populate the store)

**Goal:** the buckets contain the mirror content.

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
6. Push per the [admin-sync.md](admin-sync.md) loop: artifacts first
   (`PUT /install/admin/v1/artifact/...`), manifest last (`PUT
   /install/admin/v1/manifest`), using the master key from
   the environment (`$LITELLM_MASTER_KEY` — read it from the k8s Secret via
   `kubectl get secret litellm-master-key ...` at run time; never hardcode).

Supply-chain rules apply to the fetch script: download to file + verify
SHA-256 before use; pin every tool by exact version + full URL; never
`curl | bash`.

**Exit:** `curl -fsSL https://litellm.ecouncil.ae/install/manifest.json`
returns the real manifest; `inventory` lists all objects with matching
hashes.

---

## Phase 5 — end-to-end validation

**Goal:** a clean machine installs `lite` with zero external egress.

Per [rollout.md](rollout.md) Phase 0, hardened for this environment:

```sh
# On this workstation (which HAS internet, so use network isolation):
docker run -it --network none \
  -v /tmp/mirror-seed:/seed:ro \
  ubuntu:24.04 bash
# inside: curl against a localhost:8000 seed copy first (Phase 0 of rollout.md),
# then against the real gateway over the tunnel if reachable
```

Full e2e on the real gateway (from a machine with only
`litellm.ecouncil.ae:443` reachable, or the workstation with
`HTTPS_PROXY=http://127.0.0.1:1` to fail-closed any accidental egress):

```sh
curl -fsSL https://litellm.ecouncil.ae/install | sh
lite --version
lite login                       # or: --api-key from the env
lite configure claude            # or: lite opencode / lite codex
claude                           # a real turn through the gateway
```

**Exit:** `lite` installs, authenticates, configures the harness, and a real
agent turn routes through `/v1` with the user's key. `--no-index` resolution
shows zero external requests.

---

## Phase 6 — pilot and GA

Per [rollout.md](rollout.md). 2-3 internal users, one week, watch install
success rate / keyring / PATH quirks / `/v1` latency during parallel
installs. Then announce with the two-command story and the cautious two-step
form from [security.md](security.md).

---

## Definition of done

- Phase 1-5 exits all green, twice (dev overlay first, then prod)
- Drop-detection tests green in CI (`test_oicm_drop_detection.py`)
- `make check` passes on the touched files (the repo-wide gate)
- The mkdocs build renders this folder clean
- A real user (you) has installed `lite` via `/install` on the workstation
  and completed a real Claude Code / OpenCode turn through the gateway

## Rollback at every phase

- Phase 1: delete the StatefulSet/Service/Secret; nothing depends on it yet
- Phase 2: pure code addition; revert the commit
- Phase 3: revert the two-line wiring + env vars; the drop tests trip by
  design (that is the tripwire working)
- Phase 4: the store being empty only 404s artifact reads; `/install` script
  itself still serves (it does not read the store to render)
- Phase 5/6: installed clients keep working; `lite` talks only to `/v1`
