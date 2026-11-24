# Deploy folder: what is here, and the state it describes

Snapshot of `adeo-litellm` prod and dev as of **2026-11-19**. This file records
what the manifests say, how they are applied, and the parts that are not obvious
from reading them. When live and these files disagree, live wins and one of them
is wrong; find out which before changing anything.

## Layout

```
deploy/
├── base/          shared by both environments
│   ├── gateway/   proxy Deployment, config, secrets, kustomization
│   ├── rustfs/    rustfs StatefulSet, Service, bootstrap Job, NetworkPolicy
│   └── shared/    hooks ConfigMap
├── overlays/
│   ├── prod/      prod patches (gateway + mcp-source-broker)
│   └── dev/       dev patches, nameSuffix: -dev
├── prod/          objects applied OUTSIDE the overlay (discovery controller, postgres cluster)
├── dev/           objects applied OUTSIDE the overlay (dev equivalents)
├── oicm/          OICM tenant provisioning (not part of the gateway rollout)
└── rollback/      point-in-time snapshots with a restore script
```

## How to apply

The overlay is the single entry point for the gateway stack:

```bash
kubectl apply -k deploy/overlays/prod      # 20 objects, see below
kubectl apply -k deploy/overlays/dev
```

`kubectl apply -f <path>` takes exactly one path (a directory or a file), and
`--exclude` does not exist. `kubectl apply -f <dir>` silently skips non-YAML
files. Use `-k` on the overlay.

### What the prod overlay manages (20 objects)

```
ConfigMap/litellm-config              ConfigMap/litellm-hooks
ConfigMap/litellm-rustfs-bootstrap    Deployment/litellm-mcp-broker
Deployment/litellm-proxy              Job/litellm-rustfs-bootstrap
NetworkPolicy/litellm-mcp-broker      NetworkPolicy/litellm-rustfs-source-broker
NetworkPolicy/rustfs                  PodDisruptionBudget/litellm-proxy-pdb
Secret/litellm-db-credentials         Secret/litellm-master-key
Secret/litellm-mcp-source-broker      Secret/litellm-redis-password
Secret/litellm-rustfs-credentials     Secret/litellm-salt-key
Service/litellm-mcp-broker            Service/litellm-proxy
Service/rustfs                        StatefulSet/rustfs
```

### What the overlay does NOT manage

Applying the overlay is **not** a complete prod deploy. These are live but
outside it, and each must be applied on its own path:

| Object | File | Why outside |
| ------ | ---- | ----------- |
| `Deployment/oicm-discovery-controller` | `deploy/prod/discovery-controller.yaml` | Modelled separately; image pinned by digest |
| CNPG `Cluster/adeo-litellm-postgres` | `deploy/prod/litellm-postgres-cluster.yaml` | Database cluster lifecycle is separate from the app |
| `Ingress/litellm-proxy` | not in this folder | Created separately; see "Ingress" below |
| `Ingress/litellm-mcp-tunnel` | `deploy/prod/mcp-tunnel-ingress.yaml` | Separate object so its `GRPC` backend protocol does not apply to the gateway's Ingress |
| CNPG-generated Secrets (`*-app`, `*-server`, `*-ca`, `*-superuser`, `*-replication`) | generated | Owned by the operator, never hand-edit |

A deploy that runs only `kubectl apply -k` leaves the discovery controller on
whatever image it already had. That has happened: the controller ran stale for
weeks because nobody applied its file.

## Image pins

Prod images are pinned by **digest**, dev floats by tag. A tag can be rebuilt in
place; a digest cannot, so a prod rollout is reproducible and a dev rebuild is not.

| Component | Repo / tag | Digest |
| --------- | ---------- | ------ |
| gateway | `litellm-src:jya0-v1.108.5-prod` | `sha256:00e7e55da32094ab1e3af8a984c83fb6525f38439c2fdacbdb64d63f2554b07a` |
| broker | `litellm-mcp-broker:jya0-v1.108.5-prod` | `sha256:2728d6a738d13156e05fee5671a46c892654a50044029b391e317935a98bee89` |
| sidecar | `mcp-source-link-worker:jya0-v1.108.5-prod` | `sha256:ce3c214c205b32aaa34010fff1466ef9f8b790c7e45fc27f836d9c2b9091242c` |
| controller | `oicm-discovery-controller:0.1.0-20261123-6edfbec` | `sha256:1f37d94c50eaf49228acf945854fc21e8c16f464dfe14b72827d074013f8a9e0` |
| rustfs | `rustfs/rustfs:1.0.1` | tag only (upstream image, not rebuilt locally) |

The base gateway deployment names `litellm-src:jya0-v1.108.5` **without** a
digest. Do not apply the base alone: that tag is mutable and a dev rebuild
overwrites it, so applying base by itself can silently run a different image than
prod intended. The prod overlay overrides it with the digest above.

Verify what is running rather than trusting these lines:

```bash
kubectl -n adeo-litellm get deploy -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.template.spec.containers[*].image}{"\n"}{end}'
skopeo inspect --tls-verify=false docker://registry.adeoaiengine.ecouncil.ae/openinnovationai/platform/mlops/mlops-serving/litellm-mcp-broker:jya0-v1.108.5-prod
```

## The broker and the gateway must share one database

This is the least obvious thing in the folder and it broke prod on 2026-11-19.

The gateway (`litellm-proxy`) and the broker (`litellm-mcp-broker`) write and read
the same rows: the gateway enrolls a source and mints a worker, and the broker
looks that worker up to authorize its tunnel. If they point at different
databases, the gateway happily enrolls workers the broker never sees, the broker's
peer set is empty, and every tunnel is refused with

```
rpc error: code = Unauthenticated desc = connector unauthorized
```

which reads like a credential problem and is not one. Prod had the gateway on
database `litellm` (via `Secret/litellm-db-credentials`) and the broker on database
`oicm` (via `Secret/adeo-litellm-postgres-app`).

How each side gets its database:

| | Gateway | Broker |
| - | ------- | ------ |
| Prod | `DATABASE_URL` from `Secret/litellm-db-credentials` (`litellm`) | `DATABASE_URL` from `Secret/litellm-db-credentials` (`litellm`), **plus** unused `DATABASE_*` vars from `adeo-litellm-postgres-app` |
| Dev | `DATABASE_*` from `Secret/adeo-litellm-postgres-dev-app` | `DATABASE_*` from `Secret/adeo-litellm-postgres-dev-app` |

Dev has no such bug because one secret (`adeo-litellm-postgres-dev-app`) carries
`litellm`/`litellm` and both sides read it. Prod's `adeo-litellm-postgres-app` is
the CNPG-managed owner of the `oicm` database, which is a different database
entirely.

The broker binary prefers `DATABASE_URL` and only falls back to `DATABASE_HOST` /
`DATABASE_USERNAME` / `DATABASE_PASSWORD` / `DATABASE_NAME`
(`litellm/proxy/mcp_source_broker/main.py`). So the prod broker carries both, and
`DATABASE_URL` is the one that counts. The `DATABASE_*` vars are kept because they
are harmless and were already referenced, but do not add a *different*
`DATABASE_URL` in front of them expecting the vars to win; they lose.

Check the pairing before blaming credentials:

```bash
kubectl -n adeo-litellm get deploy litellm-mcp-broker \
  -o jsonpath='{.spec.template.spec.containers[?(@.name=="broker")].env[?(@.name=="DATABASE_URL")]}'
kubectl -n adeo-litellm get deploy litellm-proxy \
  -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="DATABASE_URL")]}'
```

## OpenAPI sources need the SSRF allowlist

`general_settings.user_url_allowed_hosts` contains `127.0.0.1:4000` in the base
config, so both environments get it.

Without it, an OpenAPI-backed MCP source is registered, approved, and then
**silently dropped** from the gateway's registry on reload. Each such source
records a `spec_path` of
`http://127.0.0.1:4000/install/mcp-source-broker/<source_id>/openapi.json`, which
is the gateway reading a catalog from itself, and the SSRF guard blocks loopback.
MCP sources carry no `spec_path`, so only they survive. The gateway log says:

```
Failed to register OpenAPI tools for server ...: URL targets a blocked address (127.0.0.1)
```

That message sent a full afternoon in the wrong direction; it looks like a client
network problem and is a server config gap.

## Ingress

`Ingress/litellm-proxy` routes **every path** on both hostnames to
`svc/litellm-proxy:4000`:

```
litellm.ecouncil.ae                 → / → svc/litellm-proxy:4000
litellm.adeoaiengine.ecouncil.ae    → / → svc/litellm-proxy:4000
```

Annotations that matter, and why:

| Annotation | Value | Why |
| ---------- | ----- | --- |
| `proxy-buffering` | `off` | Stream tokens through as packets, not buffered |
| `proxy-request-buffering` | `off` | Do not buffer request bodies |
| `proxy-max-temp-file-size` | `0` | Never spill to a temp file |
| `proxy-body-size` | `0` | No upload size cap |
| `proxy-read-timeout` / `proxy-send-timeout` | `3600` | Long generations must not be cut off |

**The tunnel is routed through the ingress on 443.** `Ingress/litellm-mcp-tunnel`
(`deploy/prod/mcp-tunnel-ingress.yaml`) sends only
`/grpctunnel.v1.TunnelService/` to `svc/litellm-mcp-broker:link-tunnel` with
`backend-protocol: GRPC`, so every other path on the same host stays on the
gateway. A client therefore needs nothing but HTTP/2 on 443:
`MCP_SOURCE_LINK_TUNNEL_URL=https://litellm.ecouncil.ae`, no port-forward.

It must be a **separate** Ingress object: `backend-protocol` is a per-Ingress
annotation, so marking the gateway's Ingress `GRPC` would force the gateway's own
HTTP traffic to gRPC as well.

## The MCP source broker overlay

`deploy/overlays/prod/mcp-source-broker/` is an overlay of its own, included by
the prod overlay. It adds the broker Deployment (two containers), its Service, its
NetworkPolicy, and a patch that mounts the sidecar.

| Container | Image | Listens |
| --------- | ----- | ------- |
| `broker` | `litellm-mcp-broker` | 9091 control (`/health`), 8888 data |
| `source-link` | `mcp-source-link-worker` | 8093 tunnel, 9092 private dispatch, 8093 reverse |

Both containers share `Secret/litellm-mcp-source-broker`:

- `pepper`: derives the per-worker tunnel credential
  (`HMAC(pepper, "remotedialer-credential", enrollment_id, worker_id, generation)`).
  Must be at least 32 bytes.
- `service-token`: the in-pod control hop between broker and sidecar. Must be at
  least **32 characters**, enforced by the sidecar; a shorter value crash-loops it.

Both are freshly generated and **neither is the gateway master key**. The service
token was once set to `sk-05132025` by instruction, which is 11 characters and
crashed the sidecar with `control URL and service token are required`. Check the
consumer's minimum before reusing a credential: verify what reads it, not just
that it looks like a key.

`MCP_SOURCE_LINK_ENABLED` is declared once per container. It was briefly declared
in both `broker.yaml` and `source-link-patch.yaml`; the patch won silently, so an
edit to the `broker.yaml` copy had no effect. If you add an env var, grep for it
across the overlay before assuming your edit is live.

## Secrets in git

`Secret/litellm-master-key`, `Secret/litellm-salt-key` and
`Secret/litellm-db-credentials` are committed in plaintext under
`deploy/base/gateway/secrets/`, and `Secret/litellm-redis-password` under
`deploy/base/shared/`. `Secret/litellm-mcp-source-broker` carries generated
values the same way. `Secret/litellm-rustfs-credentials` is declared inside
`deploy/base/rustfs/rustfs.yaml` and in `deploy/prod/litellm-redis.yaml`. All of
that is a known, accepted debt for this private repo, not a pattern to extend. A
rotated value must be changed in the file and applied, or live silently keeps the
old one.

The master key (`sk-05132025`) is the Proxy Admin credential every client uses.
Rotating it invalidates existing clients and every script that reads
`Secret/litellm-master-key`, so it is a planned action, not a drive-by fix.

## Two databases in one Postgres cluster

`adeo-litellm-postgres-1` hosts several databases, including:

- `litellm`: the gateway's, and therefore where `OicmMcp*` source rows live
- `oicm`: the CNPG bootstrap database, owned by role `oicm`, used by other tenants

`OicmMcpSource`, `OicmMcpRemoteWorker` and `OicmMcpEnrollment` exist in **both**
databases. Querying the wrong one returns zero rows and looks like data loss. The
live ones are in `litellm`. Always state the database when reporting counts:

```bash
PGPASSWORD=... psql -h 127.0.0.1 -U litellm -d litellm -c 'SELECT count(*) FROM "OicmMcpSource"'
```

Peer authentication fails for `litellm` over the unix socket; connect over
`-h 127.0.0.1` with the password from the secret.

## Rollback

`deploy/rollback/prod-live-20261119/` holds a cleaned snapshot of the running prod
state and `rollback.sh`:

```bash
bash deploy/rollback/prod-live-20261119/rollback.sh
```

It deletes the four broker objects (they did not exist at snapshot time), applies
the snapshot, waits for rollouts, and prints the resulting image IDs. It has been
executed once and converged the gateway onto its previous digest.

`deploy/rollback/gateway.yaml` is **stale**: it pins
`litellm-src:jya0-v1.106.6`, several versions behind. Do not reach for it; use the
dated directory instead.

## Dev overlay notes

Dev uses `nameSuffix: -dev`, nests the gateway in its own kustomization, and
floats images by tag. Its `config.yaml` repeats settings the base also declares
because a strategic merge replaces the whole `config.yaml` string; that repetition
is deliberate, not redundant.

Dev and prod are compared automatically by
`tests/deploy/test_dev_prod_parity.py`, which asserts the same object set modulo
the `-dev` suffix, matching images (same repo, prod digest-pinned), and an
explicit allowlist of intended env differences. Run it after touching either
overlay:

```bash
python -m pytest tests/deploy/ -q
```

## Known gaps

- The discovery controller and the ingress are outside the overlay, so a
  `kubectl apply -k` deploy is incomplete (see above).
- `mc:latest` is unpinned in the rustfs bootstrap Job. A bootstrap re-run pulls
  whatever `latest` is that day.
- The base gateway image tag is mutable; only the prod overlay pins a digest.
- Several Secrets are committed in plaintext (see "Secrets in git").
- `tests/deploy/test_source_link_ingress.py` is 1 passed, 5 errors: the erroring
  tests reference `deploy/overlays/dev/mcp-source-broker/tunnel-ingress.yaml`,
  which does not exist. They describe a tunnel ingress that was deleted, so they
  either need the feature back or need removing. Run the suite with
  `python -m pytest tests/deploy/ -q` and expect these five errors until that is
  settled.
