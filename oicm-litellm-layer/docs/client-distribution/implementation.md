# Implementation Plan

Status: Phase 1 done (RustFS deployed, per-environment). Phase 2 done (slice code,
unit-tested). Phase 3 done (wired, deployed to dev, verified). Phase 4 done
(store populated: 8 uv artifacts, 8 python archives, 77 wheels, manifest
published; /install serves the generated script on dev). v5: the generator is
now Jinja2-based with a structured render context and manifest schema 2 (see
installer-script.md "Generator contract"); the harness-detection block and the
`install_state.json` state machine were removed from the script.
Next: Phase 5 (end-to-end validation).
Step-by-step build order for the v4 design
([PLAN.md](PLAN.md), [architecture.md](architecture.md),
[rustfs.md](rustfs.md), [admin-sync.md](admin-sync.md)).

Everything below is grounded in the **current** source tree and the **live**
`adeo-litellm` namespace as of 2026-10-12:

- Gateway image: `registry.adeoaiengine.ecouncil.ae/openinnovationai/platform/mlops/mlops-serving/litellm-src:jya0-v1.102.0`
- `deployment/litellm-proxy` runs **2 replicas** (this matters: the object
  store backend is what makes multi-replica safe; a `ReadWriteOnce` PVC would
  have blocked this)
- Ingress `litellm-proxy` serves both hosts, `Prefix /` -> `litellm-proxy:4000`,
  TLS via `litellm-ecouncil-ae-tls` (dash-named secret)
- **No object store deployed yet** in `adeo-litellm`; no `NetworkPolicy`
  objects exist in the namespace at all
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
   from git, not a hand command). Where an object has a Kubernetes
   immutability limit that a plain re-apply cannot cross (a Job's
   `spec.template`), the recovery is written down next to the object rather
   than performed by hand: see "Idempotency and re-apply" in Phase 1.
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

## Phase 1 — object store deployment (no code, no risk, DEV)

**Goal:** a running S3-compatible store with both buckets, reachable only
from the gateways. Deployed to the namespace now, but nothing consumes it
until Phase 3 wires the slice; the dev gateway gets env vars first, prod's
are added only at Phase 6.

**Backend: RustFS, not MinIO.** MinIO Community Edition is end of life. The
`minio/minio` GitHub repo was archived read-only in April 2026, the Docker Hub
images were deleted in September 2026, and quay.io refuses anonymous pulls.
There is no pullable `minio/minio` at any version, so the v4 doc's
`docker.io/minio/minio:RELEASE.2026-04-01T00-00-00Z` was never obtainable.
RustFS (Apache-2.0, actively maintained) is the replacement, and it is
S3-compatible, which is the only contract LiteLLM needs: the logging path
builds a stock `boto3.client("s3", endpoint_url=...)`
(`litellm/integrations/s3.py:74`) and the v2 path hand-rolls SigV4 over raw
HTTP against `{endpoint}/{bucket}/{key}`. Neither has any MinIO-specific code.

Pinned image, already mirrored into Harbor (public `rustfs` project):

```
registry.adeoaiengine.ecouncil.ae/rustfs/rustfs:1.0.1
sha256:38fcb422c93174dada595d1aedec0e942a16a2a01a0ecca896c4a563e1f463b5
```

Everything below was verified by running that exact image locally as a
non-root, read-only-rootfs container: it starts standalone, `/health` and
`/health/ready` both return 200, boto3 S3v4 path-style put/get/list/multipart
all work, `mc mb --ignore-existing` succeeds and is re-runnable, and a bucket
re-create returns success rather than an error.

**One store per environment, not a shared one.** Dev and prod share the
`adeo-litellm` namespace, so a single unsuffixed store would give both
gateways the same StatefulSet, PVC, credentials, and buckets, and a dev sync
could write prod's data. So the store is per-environment, exactly like the
gateway: the base in `deploy/base/rustfs/` is prod's store verbatim, and the
dev overlay (`deploy/overlays/dev/rustfs/`) renames everything to `-dev`,
re-labels it, and replaces the credentials and bucket names. Dev therefore
has its own `rustfs-dev` StatefulSet, `data-rustfs-dev-0` PVC,
`litellm-rustfs-credentials-dev` Secret, and `litellm-client-mirror-dev` /
`litellm-logs-dev` buckets, and a `rustfs-dev` NetworkPolicy that admits only
`litellm-proxy-dev` and the dev store's own pods. Prod's is the symmetric
shape without the suffix.

The label handling is the subtle part, and the reason a plain `nameSuffix` is
not enough:

- `nameSuffix` renames objects but rewrites no label or selector, so on its
  own the dev StatefulSet would still carry and select `app: rustfs` (prod's
  value) and cross-select prod's pods in the shared namespace.
- A `labels` transformer cannot fix it, because it would also rewrite the
  NetworkPolicy's ingress `from` podSelector to the store's own label and
  break the allow rule (verified: `from` ended up `rustfs-dev`, allowing only
  the store to reach itself).
- So the store's label is spread by a kustomize `replacements` to exactly the
  sites that carry it (the StatefulSet and Service labels/selectors, the
  NetworkPolicy `podSelector`, and the bootstrap Job's own label), and the
  NetworkPolicy's `from` gateway label is patched per environment. The store
  and its bootstrap Job share one label, so one `from` rule admits both.

The dev parity test pins all of this: each prod store object must have a
suffixed dev counterpart, the dev store and Service must carry the dev label,
the credentials and bucket names must differ, and the NetworkPolicy must
select its own environment's store and gateway.

New manifests: the base store in `deploy/base/rustfs/rustfs.yaml` and the
dev-only overrides in `deploy/overlays/dev/rustfs/`. This is the object set,
and it is what is applied to the cluster; `kubectl apply -k
deploy/overlays/dev` is the only write path, so nothing here is created by
hand.

```yaml
apiVersion: v1
kind: Secret
metadata: {name: litellm-rustfs-credentials, namespace: adeo-litellm}
type: Opaque
stringData: {access-key: ..., secret-key: ...}   # generate, never commit real values
---
apiVersion: apps/v1
kind: StatefulSet
metadata: {name: rustfs, namespace: adeo-litellm, labels: {app: rustfs}}
spec:
  serviceName: rustfs
  replicas: 1
  selector: {matchLabels: {app: rustfs}}
  template:
    metadata: {labels: {app: rustfs}}
    spec:
      securityContext: {runAsUser: 10001, runAsGroup: 10001, fsGroup: 10001, fsGroupChangePolicy: OnRootMismatch}
      containers:
        - name: rustfs
          image: registry.adeoaiengine.ecouncil.ae/rustfs/rustfs:1.0.1
          imagePullPolicy: IfNotPresent
          securityContext:
            {readOnlyRootFilesystem: true, runAsNonRoot: true, allowPrivilegeEscalation: false,
             capabilities: {drop: [ALL]}}
          env:
            - {name: RUSTFS_ACCESS_KEY, valueFrom: {secretKeyRef: {name: litellm-rustfs-credentials, key: access-key}}}
            - {name: RUSTFS_SECRET_KEY, valueFrom: {secretKeyRef: {name: litellm-rustfs-credentials, key: secret-key}}}
            - {name: RUSTFS_VOLUMES, value: "/data"}
            - {name: RUSTFS_ADDRESS, value: "0.0.0.0:9000"}
            - {name: RUSTFS_CONSOLE_ENABLE, value: "false"}
            # The image bakes RUSTFS_OBS_LOG_DIRECTORY=/logs and a read-only rootfs
            # cannot create it, which is a FATAL startup error. An explicit empty
            # value routes logs to stdout (verified: "OBS log directory not
            # configured and logs outputs to stdout").
            - {name: RUSTFS_OBS_LOG_DIRECTORY, value: ""}
          ports: [{name: endpoint, containerPort: 9000}]
          readinessProbe: {httpGet: {path: /health/ready, port: endpoint}, initialDelaySeconds: 10, periodSeconds: 5, timeoutSeconds: 3}
          livenessProbe: {httpGet: {path: /health, port: endpoint}, initialDelaySeconds: 30, periodSeconds: 10, timeoutSeconds: 3}
          resources: {requests: {cpu: 100m, memory: 256Mi}, limits: {cpu: "1", memory: 1Gi}}
          volumeMounts: [{name: data, mountPath: /data}]
  volumeClaimTemplates:
    - metadata: {name: data}
      spec:
        accessModes: [ReadWriteOnce]
        storageClassName: longhorn-crypto-global   # existing class, referenced not created
        resources: {requests: {storage: 10Gi}}
---
apiVersion: v1
kind: Service
metadata: {name: rustfs, namespace: adeo-litellm, labels: {app: rustfs}}
spec: {selector: {app: rustfs}, ports: [{name: endpoint, port: 9000, targetPort: endpoint}]}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: litellm-rustfs-bootstrap, namespace: adeo-litellm}
data:
  bootstrap.sh: |
    #!/bin/sh
    set -eu
    : "${RUSTFS_ENDPOINT_URL:?}"; : "${RUSTFS_ACCESS_KEY:?}"; : "${RUSTFS_SECRET_KEY:?}"
    i=0
    until mc alias set rustfs "$RUSTFS_ENDPOINT_URL" "$RUSTFS_ACCESS_KEY" "$RUSTFS_SECRET_KEY" >/dev/null 2>&1; do
      i=$((i + 1))
      [ "$i" -ge 30 ] && { echo "rustfs not reachable after 30 attempts" >&2; exit 1; }
      echo "waiting for rustfs (attempt $i)"; sleep 2
    done
    for bucket in litellm-client-mirror litellm-logs; do mc mb --ignore-existing "rustfs/$bucket"; done
    mc ls rustfs
---
apiVersion: batch/v1
kind: Job
metadata: {name: litellm-rustfs-bootstrap, namespace: adeo-litellm, labels: {app: litellm-rustfs-bootstrap}}
spec:
  backoffLimit: 6
  ttlSecondsAfterFinished: 3600
  template:
    metadata: {labels: {app: litellm-rustfs-bootstrap}}
    spec:
      restartPolicy: Never
      securityContext: {runAsNonRoot: true, runAsUser: 65532, runAsGroup: 65532, fsGroup: 65532}
      containers:
        - name: mc
          image: registry.adeoaiengine.ecouncil.ae/minio/mc:latest
          command: [/bin/sh, /scripts/bootstrap.sh]
          env:
            - {name: RUSTFS_ENDPOINT_URL, value: "http://rustfs.adeo-litellm.svc.cluster.local:9000"}
            - {name: RUSTFS_ACCESS_KEY, valueFrom: {secretKeyRef: {name: litellm-rustfs-credentials, key: access-key}}}
            - {name: RUSTFS_SECRET_KEY, valueFrom: {secretKeyRef: {name: litellm-rustfs-credentials, key: secret-key}}}
            - {name: MC_CONFIG_DIR, value: /tmp/.mc}
          volumeMounts: [{name: script, mountPath: /scripts, readOnly: true}]
      volumes:
        - name: script
          configMap: {name: litellm-rustfs-bootstrap}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {name: rustfs, namespace: adeo-litellm}
spec:
  podSelector: {matchLabels: {app: rustfs}}
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector: {matchLabels: {app: litellm-proxy}}
        - podSelector: {matchLabels: {app: litellm-proxy-dev}}
        - podSelector: {matchLabels: {app: litellm-rustfs-bootstrap}}
      ports: [{protocol: TCP, port: 9000}]
```

Four things about that spec, each verified:

- **`RUSTFS_OBS_LOG_DIRECTORY` must be set empty.** The image's own `ENV`
  sets it to `/logs`, and with `readOnlyRootFilesystem: true` telemetry
  initialization fails hard (`[FATAL] Observability initialization failed:
  ... Read-only file system (os error 30)`) and the container exits 1. Empty
  is the clean fix; the alternative is an `emptyDir` at `/logs`, which works
  but emits a permission warning and needs a second volume.
- **No `/tmp` or other writable mount is needed.** Verified running with a
  read-only rootfs and only `/data` mounted.
- **No `imagePullSecrets`.** Kyverno's `add-image-pull-secrets` ClusterPolicy
  injects `registry-credentials` into every pod in this cluster, which is why
  no existing manifest declares it either.
- **The storage class is referenced, not created.** `longhorn-crypto-global`
  is an existing encrypted, Delete-reclaim class; this manifest creates no
  `StorageClass`. It was chosen over the cluster default
  `longhorn-crypto-retain` so a deleted StatefulSet does not leak its volume.
  Because the manifest is shared, prod binds the same class.

The Job and the NetworkPolicy are the two pieces that keep the deployment
reproducible:

- The **bootstrap Job** is required, not optional: RustFS has no
  `MINIO_DEFAULT_BUCKETS` equivalent, so this is the only way the buckets get
  created. It runs the `mc` client (already in Harbor at
  `registry.adeoaiengine.ecouncil.ae/minio/mc:latest`,
  `sha256:512a1cb685940ded1b0ff55470a226a3830e16305a3e0f1bfbb77056171601fd`),
  which works unchanged against RustFS. Readiness is gated on `mc alias set`
  (which fails loudly, exit 1, when the endpoint is not up) rather than
  `mc ready`, which was observed to exit 0 even against a dead endpoint and
  is therefore not a usable gate. `runAsUser: 65532` is pinned because the
  `mc` image runs as root by default and would otherwise trip `runAsNonRoot`;
  `mc` runs correctly as any non-root uid.
- The **NetworkPolicy** (the first in this namespace) allows ingress to
  `app: rustfs` port 9000 only from the prod and dev gateway pod labels
  (both verified live) plus the bootstrap Job's label. This manifest is
  shared by dev and prod by design: the object store is a common dependency,
  and the policy covering both labels is what makes the Phase 6 prod apply a
  no-surprise diff.

### Idempotency and re-apply

`kubectl apply -k deploy/overlays/dev` is safe to run any number of times.
`kubectl diff -k deploy/overlays/dev` exits 0 against the applied state, so a
re-apply changes nothing semantically.

- The Secret and the StatefulSet report `configured` on every apply (the
  API server normalizes a couple of fields), but the diff is empty, so it is
  metadata-only and not drift.
- The Job is the one object with a caveat: a Job's `spec.template` is
  **immutable**. If the bootstrap script in the ConfigMap changes, the apply
  fails with `field is immutable`. The documented recovery is delete-then-apply
  in the same round:
  `kubectl -n adeo-litellm delete job litellm-rustfs-bootstrap` then re-apply.
  `ttlSecondsAfterFinished: 3600` reaps the completed Job after an hour, so
  once the script is stable a routine re-apply needs no delete. That TTL
  matches the repo's other Jobs (`deploy/oicm/provision-alain.yaml`).
- Bucket creation itself is idempotent independently of the Job lifecycle:
  `mc mb --ignore-existing` succeeds on an existing bucket, and RustFS's own
  duplicate-bucket response is success, not error.

Steps (all dev-scoped verification; this is what was run):

1. Generate credentials; commit the Secret manifest following the same
   convention as `litellm-master-key.yaml` in this private repo.
2. `kubectl apply -k deploy/overlays/dev` (the overlay includes
   `base/shared`), then verify:
   `kubectl -n adeo-litellm rollout status statefulset/rustfs`, the Job
   completes, and the Job's log lists both buckets.
3. Verify isolation: from a pod in another namespace, the S3 endpoint must
   NOT be reachable; from a `litellm-proxy-dev` pod, it must.

**Exit:** RustFS ready, both buckets exist (created by the Job, not by hand),
gateway-only reachability confirmed. Prod is untouched: the shared manifest
is in git, but `deploy/overlays/prod` has not been applied and no env var
points prod at RustFS yet.

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

- **storage.py** reads `RUSTFS_ENDPOINT_URL`, `RUSTFS_ACCESS_KEY`,
  `RUSTFS_SECRET_KEY` (new env vars added in Phase 3). boto3 is already a
  litellm main dependency (`pyproject.toml`: `boto3>=1.43.1,<2.0`), so no new
  dependency. Use the `BaseAWSLLM`-free simple path: a plain
  `boto3.client("s3", endpoint_url=...)` created once per worker, matching how
  `litellm/integrations/s3.py` builds its client from params.
- **routes.py**:
  - `GET /install` — unauthenticated; calls `generator.render(...)`; returns
    `text/x-shellscript` with `Content-Type: application/x-sh`. Source of
    `base_url`: `LITELLM_PUBLIC_BASE_URL` env (add in Phase 3) with
    request-host fallback.
  - `GET /install/{path:path}` — unauthenticated proxy read from RustFS
    (`get_object`, stream). 404 if absent, 503 if RustFS is down. Content
    types by extension (`.json`, `.tar.gz`, `.zip`, `.whl`, `.sha256`).
  - `* /install/admin/v1/*` — `dependencies=[Depends(user_api_key_auth)]` +
    `_require_admin` copied from the `oicm_routes.py` precedent
    (`LitellmUserRoles.PROXY_ADMIN` check). Handlers: manifest GET/PUT,
    artifact PUT (stream to RustFS, sha256 on the fly, skip-if-same), artifact
    DELETE (409 if manifest-referenced), inventory GET.
  - Path containment: normalize the key against `litellm-client-mirror/`
    and reject escapes (cannot touch `litellm-logs`).
- **generator.py**: pure, no FastAPI imports. The template is a Jinja2
  document rendered with a structured context (`RenderContext`: base URL,
  versions, `platforms` list) built from manifest schema 2. Rendered with
  `SandboxedEnvironment(autoescape=False, undefined=StrictUndefined)`, so a
  template referencing an undefined variable fails `/install` loudly (500)
  rather than shipping a broken script. The template keeps only the three
  origin modifications from [installer-script.md](installer-script.md);
  resume is a version-check short-circuit (`--force` is idempotent) with no
  `install_state.json` state machine, and harness detection is removed (the
  `lite` CLI owns that).
- Follow repo code conventions: `Final` annotations, frozen dataclasses,
  `MappingProxyType`, typed everything, no `Any`. `# noqa` only with named
  rule + reason.

Tests (co-located, mirroring `tests/test_litellm/proxy/client_distribution/`):

- `test_generator.py` — render with a known manifest + base URL; assert the
  three origin overrides, the `lite config set` line, that no upstream URL
  (`astral.sh`, `github.com`, `pypi.org`) appears, and the Jinja2 contract:
  undefined variable raises, no HTML escaping of shell metacharacters, a
  `{% for %}` loop over `platforms`, and store/baked-in templates render
  identically.
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

**Goal:** the slice mounts on the app; the dev gateway can reach RustFS.

1. `proxy_server.py`: add to the fork-appended block (after line 18899, next to
   the oicm_status_router include):

```python
# OICM same-origin client distribution (/install). Co-located vertical slice.
from litellm.proxy.client_distribution.routes import router as client_distribution_router

app.include_router(client_distribution_router)
```

2. Env vars, split by the parity-test rule above:

```yaml
# BASE deployment (deploy/base/gateway/proxy/deployment.yaml) — prod values;
# dev overlay overrides each one with its own endpoint, Secret, and bucket:
- name: RUSTFS_ENDPOINT_URL
  value: "http://rustfs.adeo-litellm.svc.cluster.local:9000"
- name: RUSTFS_ACCESS_KEY
  valueFrom: {secretKeyRef: {name: litellm-rustfs-credentials, key: access-key}}
- name: RUSTFS_SECRET_KEY
  valueFrom: {secretKeyRef: {name: litellm-rustfs-credentials, key: secret-key}}
- name: RUSTFS_BUCKET
  value: "litellm-client-mirror"

# DEV overlay (deploy/overlays/dev/gateway/deployment.yaml) — overrides the
# base's prod values with dev's own store, plus the per-environment base URL:
- name: RUSTFS_ENDPOINT_URL
  value: "http://rustfs-dev.adeo-litellm.svc.cluster.local:9000"
- name: RUSTFS_ACCESS_KEY
  valueFrom: {secretKeyRef: {name: litellm-rustfs-credentials-dev, key: access-key}}
- name: RUSTFS_SECRET_KEY
  valueFrom: {secretKeyRef: {name: litellm-rustfs-credentials-dev, key: secret-key}}
- name: RUSTFS_BUCKET
  value: "litellm-client-mirror-dev"
- name: LITELLM_PUBLIC_BASE_URL
  value: "http://localhost:14001"   # dev is reached via port-forward
```

   The **parity test is the tripwire for this split.**
   `oicm-litellm-layer/tests/deploy/test_dev_prod_parity.py` fails if dev and
   prod drift in any way the overlay does not declare: env vars present in
   only one environment must be added to `_DEV_ONLY_ENV_VARS` /
   `_PROD_ONLY_ENV_VARS` with a reason, and value differences to
   `_EXPECTED_ENV_DIFFS`. It also fails on stale declarations, so every
   move between phases must update the test in the same commit.

   The clean split, given the per-environment store:

- `RUSTFS_ENDPOINT_URL` / `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY` /
  `RUSTFS_BUCKET` go in the **base** deployment with prod values (base IS
  prod). The dev overlay overrides each one with dev's own endpoint, Secret,
  and bucket name. All four are declared in `_EXPECTED_ENV_DIFFS`.
- `LITELLM_PUBLIC_BASE_URL` is **dev-only** for now (declared in
  `_DEV_ONLY_ENV_VARS`). At Phase 6 it gets its prod value
  (`https://litellm.ecouncil.ae`) in the prod overlay and moves to
  `_EXPECTED_ENV_DIFFS`.

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

The store is shared (one RustFS instance, both gateways can read it), but the
sync writes are made through the **dev gateway's** admin API so the write
path itself is what gets exercised and verified.

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
   (`PUT /install/admin/v1/artifact/...`), manifest last (`PUT /install/admin/v1/manifest`). The bearer token is the **dev** master key
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
   expects that one value difference. The `RUSTFS_*` vars are already in the
   base (Phase 3), identical for both environments. The router include is
   already in the image bytes (same tag, from the dev build). So the actual
   prod delta at this point is: the prod `LITELLM_PUBLIC_BASE_URL` value (if
   not yet present), the image pin (if the tag moved), and nothing else.
2. `kubectl apply -k deploy/overlays/prod` — one clean apply of the
   known-good manifests. This picks up: RustFS (already running, no-op
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

- Phase 1: `kubectl delete -k deploy/overlays/dev` (or git-revert + apply);
  nothing consumes the RustFS objects yet. Deleting the StatefulSet does not
  delete its PVC (Kubernetes leaves `volumeClaimTemplates` PVCs in place), so
  the data survives; the class's `Delete` reclaim policy only applies if the
  PVC itself is deleted, which is a deliberate separate step.
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
